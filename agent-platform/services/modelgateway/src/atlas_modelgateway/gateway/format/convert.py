"""转换器：只有通用入口用。账号没有 Anthropic 格式地址时，把整个请求转过去、把回答转回来。

★ 只从 Anthropic 格式往外转：不影响回答正确性的字段去掉并记下（缓存标记、思考签名）；
  会影响回答正确性但目标格式无法承载的内容（如二进制 PDF 转 OpenAI）明确拒绝，绝不静默丢弃。
★ 用量、思考字段这些各家不一样的地方问供应商方言；流式回答由 StreamTranscoder 一段段转。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from ...provider import ProviderDialect
from ...shared.kernel import ErrorCategory, Format, GatewayError, ParamPlan, Usage
from .sse import SSEEvent

__all__ = ["AnthropicToOpenAI", "Converter", "StreamTranscoder", "anthropic_usage"]

_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}
#: 单次工具调用参数最大缓冲长度（防失控输出撑爆内存）
_MAX_TOOL_ARGS_CHARS = 2_000_000


def anthropic_usage(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input,
        "cache_creation_input_tokens": u.cache_write,
        "cache_read_input_tokens": u.cache_hit,
        "output_tokens": u.output,
    }


def _tool_input(arguments: str) -> dict[str, Any]:
    """模型吐出的参数不是合法 JSON 时，原文放进专门的字段，不让整轮失败。"""
    if not arguments.strip():
        return {}
    try:
        value = json.loads(arguments)
    except ValueError:
        return {"_raw_arguments": arguments}
    return value if isinstance(value, dict) else {"_value": value}


class StreamTranscoder(Protocol):
    def feed(self, event: SSEEvent) -> list[SSEEvent]: ...

    def finish(self) -> list[SSEEvent]: ...


class Converter(Protocol):
    """入口格式 → 上游格式。request 收 ParamPlan：没有读写器的上游格式（Google）在这里写参数。"""

    entry: Format
    upstream: Format

    def request(
        self, body: Mapping[str, Any], plan: ParamPlan
    ) -> tuple[dict[str, Any], list[str]]: ...

    def response(self, data: Mapping[str, Any]) -> dict[str, Any]: ...

    def transcoder(self) -> StreamTranscoder: ...


# ════════════════════════════════════════════════════════════════════════
# Anthropic（入口）→ OpenAI（上游）
# ════════════════════════════════════════════════════════════════════════


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for b in content:
            if not isinstance(b, Mapping):
                continue
            if b.get("type") == "text":
                chunks.append(str(b.get("text", "")))
            elif b.get("type") == "document":
                src = b.get("source") or {}
                if isinstance(src, Mapping) and src.get("type") == "text":
                    chunks.append(str(src.get("data", "")))
                else:
                    raise GatewayError(
                        ErrorCategory.BEYOND_CAPABILITY,
                        "当前模型的线路是 OpenAI 格式，不支持直接读取 PDF 等二进制文档",
                        no_retry=True,
                    )
        return "".join(chunks)
    return ""


def _image_part(source: Mapping[str, Any]) -> dict[str, Any] | None:
    if source.get("type") == "base64":
        url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
    elif source.get("type") == "url":
        url = str(source.get("url", ""))
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


class AnthropicToOpenAI:
    entry = Format.ANTHROPIC
    upstream = Format.OPENAI

    def __init__(self, dialect: ProviderDialect) -> None:
        self.dialect = dialect

    # ── 请求 ────────────────────────────────────────────────────────────

    def request(
        self, body: Mapping[str, Any], plan: ParamPlan | None = None
    ) -> tuple[dict[str, Any], list[str]]:
        dropped: set[str] = set()
        messages: list[dict[str, Any]] = []
        system = body.get("system")
        if system:
            if isinstance(system, list) and any(
                isinstance(b, Mapping) and b.get("cache_control") for b in system
            ):
                dropped.add("cache_control")
            messages.append({"role": "system", "content": _text_of(system)})
        for message in body.get("messages") or []:
            messages.extend(self._message(message, dropped))
        out: dict[str, Any] = {"model": body.get("model"), "messages": messages}
        if body.get("max_tokens") is not None:
            out["max_tokens"] = body["max_tokens"]
        for key in ("temperature", "top_p"):
            if body.get(key) is not None:
                out[key] = body[key]
        if body.get("stop_sequences"):
            out["stop"] = body["stop_sequences"]
        if effort := (body.get("output_config") or {}).get("effort"):
            out["reasoning_effort"] = effort
        if body.get("tools"):
            out["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                    },
                }
                for t in body["tools"]
                if isinstance(t, Mapping) and t.get("name")
            ]
        if choice := body.get("tool_choice"):
            match choice.get("type"):
                case "auto":
                    out["tool_choice"] = "auto"
                case "any":
                    out["tool_choice"] = "required"
                case "none":
                    out["tool_choice"] = "none"
                case "tool":
                    out["tool_choice"] = {
                        "type": "function",
                        "function": {"name": choice.get("name")},
                    }
        if body.get("stream"):
            out["stream"] = True
            out["stream_options"] = {"include_usage": True}
        for key in ("thinking", "metadata", "top_k", "service_tier", "container", "mcp_servers"):
            if key in body:
                dropped.add(key)
        return out, sorted(dropped)

    def _message(self, message: Mapping[str, Any], dropped: set[str]) -> list[dict[str, Any]]:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            return [{"role": role, "content": content}]
        if not isinstance(content, list):
            return []
        if role == "assistant":
            text, calls = [], []
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    text.append(block.get("text", ""))
                elif kind == "tool_use":
                    calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
                elif kind in ("thinking", "redacted_thinking"):
                    dropped.add("thinking")
            out: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
            if calls:
                out["tool_calls"] = calls
            return [out]
        # user：工具结果拆成单独的 tool 消息，放在前面
        tools: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        for block in content:
            kind = block.get("type")
            if block.get("cache_control"):
                dropped.add("cache_control")
            if kind == "tool_result":
                tools.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        "content": _text_of(block.get("content"))
                        + (" [工具报错]" if block.get("is_error") else ""),
                    }
                )
                if isinstance(block.get("content"), list):
                    for inner in block["content"]:
                        if isinstance(inner, Mapping) and inner.get("type") == "image":
                            image = _image_part(inner.get("source") or {})
                            if image:
                                parts.append(image)
            elif kind == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif kind == "image":
                image = _image_part(block.get("source") or {})
                if image:
                    parts.append(image)
            elif kind == "document":
                src = block.get("source") or {}
                if isinstance(src, Mapping) and src.get("type") == "text":
                    parts.append({"type": "text", "text": str(src.get("data", ""))})
                else:
                    raise GatewayError(
                        ErrorCategory.BEYOND_CAPABILITY,
                        "当前模型的线路是 OpenAI 格式，不支持直接读取 PDF 等二进制文档",
                        no_retry=True,
                    )
            else:
                dropped.add(str(kind))
        out_messages = list(tools)
        if parts:
            if all(p["type"] == "text" for p in parts):
                out_messages.append({"role": "user", "content": "".join(p["text"] for p in parts)})
            else:
                out_messages.append({"role": "user", "content": parts})
        return out_messages

    # ── 非流式回答 ──────────────────────────────────────────────────────

    def response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        blocks: list[dict[str, Any]] = []
        if reasoning := self.dialect.reasoning(message):
            blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
        if message.get("content"):
            blocks.append({"type": "text", "text": message["content"]})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fn.get("name"),
                    "input": _tool_input(fn.get("arguments") or ""),
                }
            )
        usage = self.dialect.usage(Format.OPENAI, data.get("usage") or {})
        return {
            "id": data.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": data.get("model"),
            "content": blocks,
            "stop_reason": _STOP.get(str(choice.get("finish_reason")), "end_turn"),
            "stop_sequence": None,
            "usage": anthropic_usage(usage),
        }

    def transcoder(self) -> StreamTranscoder:
        return _OpenAIToAnthropicStream(self.dialect)


class _OpenAIToAnthropicStream:
    """OpenAI 的增量 → Anthropic 的块：记着现在在写哪一块，内容类型一变就关上旧块、开新块。

    工具参数先在有界缓冲区攒着，块关上时校验 JSON 后一次给出（非法 JSON 兜底放进 _raw_arguments，
    避免开源模型拼错 JSON 让整轮失败；攒参数期间外层流泵按 keepalive_s 发保活注释）。
    停止原因和用量在最后一段给出。
    """

    def __init__(self, dialect: ProviderDialect) -> None:
        self.dialect = dialect
        self.started = False
        self.index = -1
        self.open: str | None = None  # "text" | "thinking" | "tool"
        self.tool_index: int | None = None
        self.tool_args: list[str] = []
        self.tool_args_len = 0
        self.usage = Usage()
        self.finish_reason: str | None = None
        self.finished = False

    def _start(self, data: Mapping[str, Any]) -> list[SSEEvent]:
        self.started = True
        message = {
            "id": data.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": data.get("model"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": anthropic_usage(Usage()),
        }
        return [SSEEvent.of({"type": "message_start", "message": message}, "message_start")]

    def _close(self) -> list[SSEEvent]:
        if self.open is None:
            return []
        out: list[SSEEvent] = []
        if self.open == "thinking":
            out.append(self._delta({"type": "signature_delta", "signature": ""}))
        elif self.open == "tool":
            payload = json.dumps(_tool_input("".join(self.tool_args)), ensure_ascii=False)
            out.append(self._delta({"type": "input_json_delta", "partial_json": payload}))
            self.tool_args = []
            self.tool_args_len = 0
            self.tool_index = None
        out.append(
            SSEEvent.of({"type": "content_block_stop", "index": self.index}, "content_block_stop")
        )
        self.open = None
        return out

    def _open(self, kind: str, block: dict[str, Any]) -> list[SSEEvent]:
        out = self._close()
        self.index += 1
        self.open = kind
        out.append(
            SSEEvent.of(
                {"type": "content_block_start", "index": self.index, "content_block": block},
                "content_block_start",
            )
        )
        return out

    def _delta(self, delta: dict[str, Any]) -> SSEEvent:
        return SSEEvent.of(
            {"type": "content_block_delta", "index": self.index, "delta": delta},
            "content_block_delta",
        )

    def feed(self, event: SSEEvent) -> list[SSEEvent]:
        if event.done:
            return self.finish()
        data = event.json()
        if not isinstance(data, Mapping):
            return []
        if "error" in data:
            err = data["error"]
            body = err if isinstance(err, Mapping) else {"message": str(err)}
            return [SSEEvent.of({"type": "error", "error": body}, "error")]
        out = [] if self.started else self._start(data)
        if data.get("usage"):
            self.usage = self.usage.merge(self.dialect.usage(Format.OPENAI, data["usage"]))
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            if reasoning := self.dialect.reasoning(delta):
                if self.open != "thinking":
                    out += self._open(
                        "thinking", {"type": "thinking", "thinking": "", "signature": ""}
                    )
                out.append(self._delta({"type": "thinking_delta", "thinking": reasoning}))
            if delta.get("content"):
                if self.open != "text":
                    out += self._open("text", {"type": "text", "text": ""})
                out.append(self._delta({"type": "text_delta", "text": delta["content"]}))
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                fn = call.get("function") or {}
                if self.open != "tool" or index != self.tool_index:
                    out += self._open(
                        "tool",
                        {
                            "type": "tool_use",
                            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": fn.get("name"),
                            "input": {},
                        },
                    )
                    self.tool_index = index
                if fn.get("arguments") and self.tool_args_len < _MAX_TOOL_ARGS_CHARS:
                    chunk = str(fn["arguments"])
                    self.tool_args.append(chunk)
                    self.tool_args_len += len(chunk)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return out

    def finish(self) -> list[SSEEvent]:
        if self.finished:
            return []
        self.finished = True
        out = [] if self.started else self._start({})
        out += self._close()
        out.append(
            SSEEvent.of(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _STOP.get(str(self.finish_reason), "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": anthropic_usage(self.usage),
                },
                "message_delta",
            )
        )
        out.append(SSEEvent.of({"type": "message_stop"}, "message_stop"))
        return out
