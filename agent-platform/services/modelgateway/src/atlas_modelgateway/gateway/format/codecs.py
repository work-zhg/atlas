"""读写器：每种入口格式一个，只读写少数字段，不做整体转换。

    summarize   读出共同信息（模型名、会话标识、意图、有没有图片和工具、大致长度）
    apply       按 ParamPlan 在原格式上改参数
    outgoing    回答写回调用方前的小改动：模型名换回调用时的名字、思考块打来源标记
    inspect     看一段回答：是不是内容、带没带用量、是不是错误
    error_*     把网关的错误写成这种格式

★ 只按入口格式读：转换过的回答已经是入口格式。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..kernel import ErrorCategory, GatewayError, Intent, ModelKind, ParamPlan, RequestSummary
from .sse import SSEEvent

__all__ = [
    "SESSION_HEADER",
    "STAMP_PREFIX",
    "AnthropicCodec",
    "FormatCodec",
    "OpenAICodec",
    "OutContext",
    "Peek",
    "unwrap_signature",
    "wrap_signature",
]

#: 会话标识请求头：同一会话落在同一条线路上（不带就按 Key 算）
SESSION_HEADER = "x-session-id"
#: 思考块签名前的来源标记：atlas1:<来源>:<原签名>
STAMP_PREFIX = "atlas1:"
#: 估算 token：按字符数 / 3（中文偏少估、英文偏多估，宁可放过不误拒）
_CHARS_PER_TOKEN = 3


def wrap_signature(stamp: str, original: str) -> str:
    return f"{STAMP_PREFIX}{stamp}:{original}" if stamp else original


def unwrap_signature(signature: str) -> tuple[str | None, str]:
    """→ (来源标记, 原签名)；没有标记的是 (None, 原样)。"""
    if not signature.startswith(STAMP_PREFIX):
        return None, signature
    stamp, _, original = signature[len(STAMP_PREFIX) :].partition(":")
    return stamp, original


@dataclass(frozen=True)
class OutContext:
    #: 调用时的模型名（上游模型名要换回它）
    model: str
    stamp: str = ""
    #: OpenAI 格式：调用方自己要了流式用量
    caller_wants_usage: bool = True


@dataclass(frozen=True)
class Peek:
    """一段回答的要点（入口格式）。"""

    is_content: bool = False
    usage: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    stop_reason: str | None = None
    #: 这一段不转给调用方（OpenAI 格式里调用方没要的用量段）
    drop: bool = False


class FormatCodec(Protocol):
    def summarize(
        self, body: Mapping[str, Any], headers: Mapping[str, str], kind: ModelKind
    ) -> RequestSummary: ...

    def apply(self, body: Mapping[str, Any], plan: ParamPlan) -> dict[str, Any]: ...

    def outgoing(self, event: SSEEvent, ctx: OutContext) -> SSEEvent: ...

    def outgoing_body(self, data: dict[str, Any], ctx: OutContext) -> dict[str, Any]: ...

    def inspect(self, event: SSEEvent, ctx: OutContext) -> Peek: ...

    def body_usage(self, data: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def error_body(self, err: GatewayError) -> dict[str, Any]: ...

    def error_event(self, err: GatewayError) -> SSEEvent: ...


def _text_len(value: Any) -> int:
    """估算长度时只数文字，跳过图片、文件的 base64。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_len(v) for v in value)
    if isinstance(value, Mapping):
        if value.get("type") in ("base64",) or ("data" in value and value.get("type") == "base64"):
            return 0
        return sum(_text_len(v) for k, v in value.items() if k not in ("data", "url", "image_url"))
    return 0


def _session(headers: Mapping[str, str], fallback: Any) -> str | None:
    lower = {k.lower(): v for k, v in headers.items()}
    value = lower.get(SESSION_HEADER) or fallback
    return str(value) if value else None


# ════════════════════════════════════════════════════════════════════════
# Anthropic 格式
# ════════════════════════════════════════════════════════════════════════

_ANTHROPIC_ERROR_TYPE = {
    ErrorCategory.KEY_INVALID: "authentication_error",
    ErrorCategory.MODEL_FORBIDDEN: "permission_error",
    ErrorCategory.BEYOND_CAPABILITY: "invalid_request_error",
    ErrorCategory.FORMAT_UNSUPPORTED: "invalid_request_error",
    ErrorCategory.BAD_REQUEST: "invalid_request_error",
    ErrorCategory.KEY_LIMIT: "rate_limit_error",
    ErrorCategory.MODEL_BUSY: "rate_limit_error",
    ErrorCategory.NO_ROUTE: "overloaded_error",
    ErrorCategory.UPSTREAM_ERROR: "api_error",
    ErrorCategory.UPSTREAM_TIMEOUT: "api_error",
    ErrorCategory.INTERRUPTED: "api_error",
}

_CONTENT_BLOCKS = ("text", "thinking", "redacted_thinking", "tool_use", "server_tool_use")


class AnthropicCodec:
    def summarize(
        self, body: Mapping[str, Any], headers: Mapping[str, str], kind: ModelKind
    ) -> RequestSummary:
        thinking = body.get("thinking") or {}
        mode = thinking.get("type") if isinstance(thinking, Mapping) else None
        intent = Intent(
            thinking={"adaptive": "auto", "enabled": "on", "disabled": "off"}.get(str(mode)),
            thinking_budget=thinking.get("budget_tokens")
            if isinstance(thinking, Mapping)
            else None,
            effort=(body.get("output_config") or {}).get("effort"),
            temperature=body.get("temperature"),
            max_output=body.get("max_tokens"),
        )
        has_images, stamps = False, set()
        for message in body.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                kind_ = block.get("type") if isinstance(block, Mapping) else None
                if kind_ in ("image", "document"):
                    has_images = True
                elif kind_ == "tool_result" and isinstance(block.get("content"), list):
                    has_images |= any(
                        isinstance(b, Mapping) and b.get("type") in ("image", "document")
                        for b in block["content"]
                    )
                elif kind_ == "thinking":
                    stamp, _ = unwrap_signature(str(block.get("signature") or ""))
                    if stamp:
                        stamps.add(stamp)
        text = (
            _text_len(body.get("system"))
            + _text_len(body.get("messages"))
            + _text_len(body.get("tools"))
        )
        return RequestSummary(
            model=str(body.get("model") or ""),
            kind=kind,
            stream=bool(body.get("stream")),
            session_key=_session(headers, (body.get("metadata") or {}).get("user_id")),
            intent=intent,
            has_images=has_images,
            has_tools=bool(body.get("tools")),
            approx_input_tokens=text // _CHARS_PER_TOKEN,
            thinking_stamps=frozenset(stamps),
        )

    def apply(self, body: Mapping[str, Any], plan: ParamPlan) -> dict[str, Any]:
        out = dict(body)
        out["model"] = plan.upstream_model
        match plan.thinking:
            case "adaptive":
                out["thinking"] = {"type": "adaptive"}
            case "budget":
                out["thinking"] = {"type": "enabled", "budget_tokens": plan.thinking_budget}
            case "off":
                out["thinking"] = {"type": "disabled"}
            case _:
                out.pop("thinking", None)
        config = dict(out.get("output_config") or {})
        if plan.effort:
            config["effort"] = plan.effort
        else:
            config.pop("effort", None)
        if config:
            out["output_config"] = config
        else:
            out.pop("output_config", None)
        if plan.temperature is None:
            out.pop("temperature", None)
        else:
            out["temperature"] = plan.temperature
        if plan.max_output is not None:
            out["max_tokens"] = plan.max_output
        for name in plan.drop_fields:
            out.pop(name, None)
        if "messages" in out:
            out["messages"] = self._history(out["messages"], plan)
        return out

    @staticmethod
    def _history(messages: list[Any], plan: ParamPlan) -> list[Any]:
        """历史里的思考块：同一个上游模型产生的按规则带回（去掉标记），别的上游产生的去掉。"""
        result: list[Any] = []
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list) or not any(
                isinstance(b, Mapping) and b.get("type") in ("thinking", "redacted_thinking")
                for b in content
            ):
                result.append(message)
                continue
            blocks = []
            for block in content:
                kind = block.get("type") if isinstance(block, Mapping) else None
                if kind == "thinking":
                    stamp, original = unwrap_signature(str(block.get("signature") or ""))
                    if plan.strip_history_thinking or (stamp is not None and stamp != plan.stamp):
                        continue
                    if stamp is not None and not original:
                        continue  # 转换来的思考（上游没有签名）带不回去
                    blocks.append({**block, "signature": original})
                elif kind == "redacted_thinking":
                    if not plan.strip_history_thinking:
                        blocks.append(block)
                else:
                    blocks.append(block)
            result.append({**message, "content": blocks or [{"type": "text", "text": ""}]})
        return result

    def outgoing(self, event: SSEEvent, ctx: OutContext) -> SSEEvent:
        name = event.event
        if name == "message_start":
            data = event.json()
            message = dict(data.get("message") or {})
            message["model"] = ctx.model
            return SSEEvent.of({**data, "message": message}, name)
        if name == "content_block_delta" and ctx.stamp and '"signature_delta"' in event.data:
            data = event.json()
            delta = dict(data.get("delta") or {})
            delta["signature"] = wrap_signature(ctx.stamp, str(delta.get("signature") or ""))
            return SSEEvent.of({**data, "delta": delta}, name)
        return event

    def outgoing_body(self, data: dict[str, Any], ctx: OutContext) -> dict[str, Any]:
        out = {**data, "model": ctx.model}
        if ctx.stamp and isinstance(data.get("content"), list):
            out["content"] = [
                {**b, "signature": wrap_signature(ctx.stamp, str(b.get("signature") or ""))}
                if isinstance(b, Mapping) and b.get("type") == "thinking"
                else b
                for b in data["content"]
            ]
        return out

    def inspect(self, event: SSEEvent, ctx: OutContext) -> Peek:
        match event.event:
            case "content_block_delta":
                return Peek(is_content=True)
            case "content_block_start":
                block = (event.json().get("content_block") or {}).get("type")
                return Peek(is_content=block in _CONTENT_BLOCKS)
            case "message_start":
                return Peek(usage=(event.json().get("message") or {}).get("usage"))
            case "message_delta":
                data = event.json()
                return Peek(
                    usage=data.get("usage"),
                    stop_reason=(data.get("delta") or {}).get("stop_reason"),
                )
            case "error":
                return Peek(error=event.json())
            case _:
                return Peek()

    def body_usage(self, data: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return data.get("usage")

    def error_body(self, err: GatewayError) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {
                "type": _ANTHROPIC_ERROR_TYPE[err.category],
                "message": err.message,
                # 调用方按类别给提示（Key 限额用完换模型没用，模型繁忙换模型有用）
                "category": err.category.value,
            },
        }

    def error_event(self, err: GatewayError) -> SSEEvent:
        return SSEEvent.of(self.error_body(err), "error")


# ════════════════════════════════════════════════════════════════════════
# OpenAI 格式（Chat Completions）
# ════════════════════════════════════════════════════════════════════════

_OPENAI_ERROR_TYPE = {
    ErrorCategory.KEY_INVALID: "authentication_error",
    ErrorCategory.MODEL_FORBIDDEN: "permission_error",
    ErrorCategory.BEYOND_CAPABILITY: "invalid_request_error",
    ErrorCategory.FORMAT_UNSUPPORTED: "invalid_request_error",
    ErrorCategory.BAD_REQUEST: "invalid_request_error",
    ErrorCategory.KEY_LIMIT: "rate_limit_error",
    ErrorCategory.MODEL_BUSY: "rate_limit_error",
    ErrorCategory.NO_ROUTE: "service_unavailable",
    ErrorCategory.UPSTREAM_ERROR: "server_error",
    ErrorCategory.UPSTREAM_TIMEOUT: "server_error",
    ErrorCategory.INTERRUPTED: "server_error",
}


class OpenAICodec:
    def summarize(
        self, body: Mapping[str, Any], headers: Mapping[str, str], kind: ModelKind
    ) -> RequestSummary:
        effort = body.get("reasoning_effort")
        has_images = False
        for message in body.get("messages") or []:
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list) and any(
                isinstance(p, Mapping) and p.get("type") in ("image_url", "input_image", "file")
                for p in content
            ):
                has_images = True
        text = (
            _text_len(body.get("messages"))
            + _text_len(body.get("tools"))
            + _text_len(body.get("input"))
        )
        return RequestSummary(
            model=str(body.get("model") or ""),
            kind=kind,
            stream=bool(body.get("stream")),
            session_key=_session(headers, body.get("user")),
            intent=Intent(
                thinking="off" if effort in ("none", "minimal") else None,
                effort=effort if effort not in (None, "none", "minimal") else None,
                temperature=body.get("temperature"),
                max_output=body.get("max_completion_tokens") or body.get("max_tokens"),
            ),
            has_images=has_images,
            has_tools=bool(body.get("tools") or body.get("functions")),
            approx_input_tokens=text // _CHARS_PER_TOKEN,
        )

    def apply(self, body: Mapping[str, Any], plan: ParamPlan) -> dict[str, Any]:
        out = dict(body)
        out["model"] = plan.upstream_model
        if plan.effort:
            out["reasoning_effort"] = plan.effort
        elif plan.thinking != "off":
            out.pop("reasoning_effort", None)
        if plan.temperature is None:
            out.pop("temperature", None)
        else:
            out["temperature"] = plan.temperature
        if plan.max_output is not None:
            key = "max_completion_tokens" if "max_completion_tokens" in out else "max_tokens"
            out[key] = plan.max_output
        for name in plan.drop_fields:
            out.pop(name, None)
        if out.get("stream"):
            # 用量只在最后一段给，有的供应商默认不给：要求它带上
            out["stream_options"] = {**(out.get("stream_options") or {}), "include_usage": True}
        return out

    def outgoing(self, event: SSEEvent, ctx: OutContext) -> SSEEvent:
        if event.done:
            return event
        data = event.json()
        if isinstance(data, dict) and "model" in data:
            return SSEEvent.of({**data, "model": ctx.model}, event.event)
        return event

    def outgoing_body(self, data: dict[str, Any], ctx: OutContext) -> dict[str, Any]:
        return {**data, "model": ctx.model} if "model" in data else data

    def inspect(self, event: SSEEvent, ctx: OutContext) -> Peek:
        if event.done:
            return Peek()
        data = event.json()
        if not isinstance(data, Mapping):
            return Peek()
        if "error" in data:
            return Peek(error=data)
        choices = data.get("choices") or []
        content = any(
            (c.get("delta") or {}).get(k)
            for c in choices
            for k in ("content", "tool_calls", "reasoning_content", "reasoning", "refusal")
        )
        stop = next((c.get("finish_reason") for c in choices if c.get("finish_reason")), None)
        usage = data.get("usage")
        return Peek(
            is_content=bool(content),
            usage=usage,
            stop_reason=stop,
            drop=bool(usage) and not choices and not ctx.caller_wants_usage,
        )

    def body_usage(self, data: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return data.get("usage")

    def error_body(self, err: GatewayError) -> dict[str, Any]:
        return {
            "error": {
                "message": err.message,
                "type": _OPENAI_ERROR_TYPE[err.category],
                "code": err.category.value,
            }
        }

    def error_event(self, err: GatewayError) -> SSEEvent:
        return SSEEvent.of(self.error_body(err))


def deep_copy(body: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(body))
