"""Anthropic（入口）→ Google（Gemini 原生接口）。

    请求    system → systemInstruction；消息 → contents（assistant 叫 model）；
            文字、图片与 base64 文档（inlineData，含 PDF）、
            工具调用（functionCall）、工具结果（functionResponse，函数名从前面的工具调用块查）；
            tools → functionDeclarations（参数 schema 去掉 Gemini 不收的字段）；
            最大输出、温度、停止词、思考（thinkingConfig）写进 generationConfig
    回答    文字（thought=true 的是思考）、functionCall → 工具调用块；
            finishReason、usageMetadata 换过来
    去掉    缓存标记、思考块的签名；不支持的外部 URL 文档明确报错

★ 没有 Google 的读写器：参数在这里按 ParamPlan 写。模型名在地址里（方言拼地址）。
★ 各家写法以文档和实测为准：Gemini 的 schema 只认 OpenAPI 子集，思考额度 -1 = 由模型自己定。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from ...provider import ProviderDialect
from ...shared.kernel import ErrorCategory, Format, GatewayError, ParamPlan, Usage
from .convert import StreamTranscoder, anthropic_usage
from .sse import SSEEvent

__all__ = ["AnthropicToGoogle"]

_FINISH = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
}
#: Gemini 的函数参数只认 OpenAPI 子集
_SCHEMA_DROP = {
    "$schema",
    "additionalProperties",
    "$defs",
    "definitions",
    "$ref",
    "examples",
    "default",
}


def _schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _schema(v) for k, v in value.items() if k not in _SCHEMA_DROP}
    if isinstance(value, list):
        return [_schema(v) for v in value]
    return value


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "")
        for b in content or []
        if isinstance(b, Mapping) and b.get("type") == "text"
    )


def _doc_part(source: Mapping[str, Any]) -> dict[str, Any]:
    if source.get("type") == "base64":
        return {
            "inlineData": {
                "mimeType": source.get("media_type", "application/pdf"),
                "data": source.get("data", ""),
            }
        }
    if source.get("type") == "text":
        return {"text": str(source.get("data", ""))}
    raise GatewayError(
        ErrorCategory.BEYOND_CAPABILITY,
        "当前模型的 Google 格式线路只支持 base64 或纯文本的文档块",
        no_retry=True,
    )


class AnthropicToGoogle:
    entry = Format.ANTHROPIC
    upstream = Format.GOOGLE

    def __init__(self, dialect: ProviderDialect) -> None:
        self.dialect = dialect

    def request(self, body: Mapping[str, Any], plan: ParamPlan) -> tuple[dict[str, Any], list[str]]:
        dropped: set[str] = set()
        names: dict[str, str] = {}  # 工具调用 id → 函数名
        contents: list[dict[str, Any]] = []
        for m in body.get("messages") or []:
            role = "model" if m.get("role") == "assistant" else "user"
            content = m.get("content")
            blocks = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else list(content or [])
            )
            parts: list[dict[str, Any]] = []
            for b in blocks:
                kind = b.get("type")
                if b.get("cache_control"):
                    dropped.add("cache_control")
                if kind == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif kind == "image" and (b.get("source") or {}).get("type") == "base64":
                    src = b["source"]
                    parts.append(
                        {
                            "inlineData": {
                                "mimeType": src.get("media_type", "image/png"),
                                "data": src.get("data", ""),
                            }
                        }
                    )
                elif kind == "document":
                    parts.append(_doc_part(b.get("source") or {}))
                elif kind == "tool_use":
                    names[str(b.get("id"))] = str(b.get("name"))
                    parts.append(
                        {"functionCall": {"name": b.get("name"), "args": b.get("input") or {}}}
                    )
                elif kind == "tool_result":
                    name = names.get(str(b.get("tool_use_id")), "tool")
                    parts.append(
                        {
                            "functionResponse": {
                                "name": name,
                                "response": {"content": _text(b.get("content"))},
                            }
                        }
                    )
                    if isinstance(b.get("content"), list):
                        for inner in b["content"]:
                            if not isinstance(inner, Mapping):
                                continue
                            if (
                                inner.get("type") == "image"
                                and (inner.get("source") or {}).get("type") == "base64"
                            ):
                                src = inner["source"]
                                parts.append(
                                    {
                                        "inlineData": {
                                            "mimeType": src.get("media_type", "image/png"),
                                            "data": src.get("data", ""),
                                        }
                                    }
                                )
                            elif inner.get("type") == "document":
                                parts.append(_doc_part(inner.get("source") or {}))
                else:
                    dropped.add(str(kind))
            if parts:
                if contents and contents[-1]["role"] == role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": role, "parts": parts})
        out: dict[str, Any] = {"contents": contents}
        if system := _text(body.get("system")):
            out["systemInstruction"] = {"parts": [{"text": system}]}
        if body.get("tools"):
            out["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            "parameters": _schema(
                                t.get("input_schema") or {"type": "object", "properties": {}}
                            ),
                        }
                        for t in body["tools"]
                        if isinstance(t, Mapping) and t.get("name")
                    ]
                }
            ]
        if choice := body.get("tool_choice"):
            mode = {"auto": "AUTO", "any": "ANY", "none": "NONE", "tool": "ANY"}.get(
                str(choice.get("type")), "AUTO"
            )
            config: dict[str, Any] = {"mode": mode}
            if choice.get("type") == "tool":
                config["allowedFunctionNames"] = [choice.get("name")]
            out["toolConfig"] = {"functionCallingConfig": config}
        generation: dict[str, Any] = {}
        if plan.max_output or body.get("max_tokens"):
            generation["maxOutputTokens"] = plan.max_output or body.get("max_tokens")
        if plan.temperature is not None:
            generation["temperature"] = plan.temperature
        if body.get("stop_sequences"):
            generation["stopSequences"] = body["stop_sequences"]
        match plan.thinking:
            case "adaptive":
                generation["thinkingConfig"] = {"thinkingBudget": -1, "includeThoughts": True}
            case "budget":
                generation["thinkingConfig"] = {
                    "thinkingBudget": plan.thinking_budget,
                    "includeThoughts": True,
                }
            case "off":
                generation["thinkingConfig"] = {"thinkingBudget": 0}
        if generation:
            out["generationConfig"] = generation
        return out, sorted(dropped)

    def _blocks(self, parts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for p in parts:
            if "functionCall" in p:
                call = p["functionCall"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{uuid.uuid4().hex[:24]}",
                        "name": call.get("name"),
                        "input": call.get("args") or {},
                    }
                )
            elif p.get("thought") and p.get("text"):
                blocks.append({"type": "thinking", "thinking": p["text"], "signature": ""})
            elif p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
        return blocks

    def response(self, data: Mapping[str, Any]) -> dict[str, Any]:
        candidate = (data.get("candidates") or [{}])[0]
        blocks = self._blocks((candidate.get("content") or {}).get("parts") or [])
        stop = (
            "tool_use"
            if any(b["type"] == "tool_use" for b in blocks)
            else _FINISH.get(str(candidate.get("finishReason")), "end_turn")
        )
        usage = self.dialect.usage(Format.GOOGLE, data.get("usageMetadata") or {})
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": data.get("modelVersion"),
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": anthropic_usage(usage),
        }

    def transcoder(self) -> StreamTranscoder:
        return _GoogleToAnthropicStream(self)


class _GoogleToAnthropicStream:
    """Gemini 的流式（alt=sse）每段是一个完整的 GenerateContentResponse 片段：文字一段段来，
    函数调用整个来，用量是累计的。"""

    def __init__(self, conv: AnthropicToGoogle) -> None:
        self.conv = conv
        self.started = False
        self.index = -1
        self.open: str | None = None
        self.usage = Usage()
        self.stop: str | None = None
        self.used_tool = False
        self.done = False

    def _start(self, model: Any) -> list[SSEEvent]:
        self.started = True
        message = {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": anthropic_usage(Usage()),
        }
        return [SSEEvent.of({"type": "message_start", "message": message}, "message_start")]

    def _close(self) -> list[SSEEvent]:
        if self.open is None:
            return []
        out = []
        if self.open == "thinking":
            out.append(
                SSEEvent.of(
                    {
                        "type": "content_block_delta",
                        "index": self.index,
                        "delta": {"type": "signature_delta", "signature": ""},
                    },
                    "content_block_delta",
                )
            )
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
        data = event.json()
        if not isinstance(data, Mapping):
            return []
        if "error" in data:
            return [SSEEvent.of({"type": "error", "error": data["error"]}, "error")]
        out = [] if self.started else self._start(data.get("modelVersion"))
        if data.get("usageMetadata"):
            self.usage = self.conv.dialect.usage(Format.GOOGLE, data["usageMetadata"])
        for candidate in data.get("candidates") or []:
            for p in (candidate.get("content") or {}).get("parts") or []:
                if "functionCall" in p:
                    call = p["functionCall"]
                    self.used_tool = True
                    out += self._open(
                        "tool",
                        {
                            "type": "tool_use",
                            "id": f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": call.get("name"),
                            "input": {},
                        },
                    )
                    out.append(
                        self._delta(
                            {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(
                                    call.get("args") or {}, ensure_ascii=False
                                ),
                            }
                        )
                    )
                    out += self._close()
                elif p.get("thought") and p.get("text"):
                    if self.open != "thinking":
                        out += self._open(
                            "thinking", {"type": "thinking", "thinking": "", "signature": ""}
                        )
                    out.append(self._delta({"type": "thinking_delta", "thinking": p["text"]}))
                elif p.get("text"):
                    if self.open != "text":
                        out += self._open("text", {"type": "text", "text": ""})
                    out.append(self._delta({"type": "text_delta", "text": p["text"]}))
            if candidate.get("finishReason"):
                self.stop = candidate["finishReason"]
        return out

    def finish(self) -> list[SSEEvent]:
        if self.done:
            return []
        self.done = True
        out = [] if self.started else self._start(None)
        out += self._close()
        stop = "tool_use" if self.used_tool else _FINISH.get(str(self.stop), "end_turn")
        out.append(
            SSEEvent.of(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": anthropic_usage(self.usage),
                },
                "message_delta",
            )
        )
        out.append(SSEEvent.of({"type": "message_stop"}, "message_stop"))
        return out
