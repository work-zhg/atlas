"""供应商方言：同一种格式下，一家供应商和标准写法不一样的地方。

★ 只做翻译、不做决定：报错归成统一的种类（kernel.FailureKind），用量换算成四项，参数改成这家的
  字段名。换不换线路由线路领域决定，怎么算钱由计量决定。
★ StandardDialect 就是标准写法，大多数供应商只用它；不一样的地方由子类覆盖（dialects.py）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..gateway.kernel import AccountRef, FailureKind, Format, ParamPlan, UpstreamFailure, Usage
from .spec import AuthStyle, ProviderSpec

__all__ = ["Op", "ProviderDialect", "StandardDialect", "error_fields", "join_url"]

ANTHROPIC_VERSION = "2023-06-01"


class Op(StrEnum):
    """要调上游的哪个接口。"""

    MESSAGES = "messages"
    COUNT_TOKENS = "count_tokens"
    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"
    MODELS = "models"
    GENERATE = "generate"
    STREAM_GENERATE = "stream_generate"


_PATHS: dict[tuple[Format, Op], str] = {
    (Format.ANTHROPIC, Op.MESSAGES): "/v1/messages",
    (Format.ANTHROPIC, Op.COUNT_TOKENS): "/v1/messages/count_tokens",
    (Format.ANTHROPIC, Op.MODELS): "/v1/models",
    (Format.OPENAI, Op.CHAT): "/chat/completions",
    (Format.OPENAI, Op.EMBEDDINGS): "/embeddings",
    (Format.OPENAI, Op.RERANK): "/rerank",
    (Format.OPENAI, Op.MODELS): "/models",
    (Format.GOOGLE, Op.GENERATE): "/models/{model}:generateContent",
    (Format.GOOGLE, Op.STREAM_GENERATE): "/models/{model}:streamGenerateContent?alt=sse",
    (Format.GOOGLE, Op.MODELS): "/models",
}


def join_url(base: str, path: str, model: str = "") -> str:
    """地址 + 接口路径。地址里可以带 {model} 和查询串（Azure 的 api-version），
    路径插在查询串前面。"""
    base = base.replace("{model}", model)
    path = path.replace("{model}", model)
    b, p = urlsplit(base), urlsplit(path)
    query = parse_qsl(b.query, keep_blank_values=True) + parse_qsl(p.query, keep_blank_values=True)
    return urlunsplit((b.scheme, b.netloc, b.path.rstrip("/") + p.path, urlencode(query), ""))


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, Mapping):
            data = data.get(part)
        else:
            return None
    return data


def _json(body: bytes | Mapping[str, Any] | None) -> Any:
    if body is None or isinstance(body, Mapping):
        return body
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None


def error_fields(body: bytes | Mapping[str, Any] | None) -> tuple[str, str, str]:
    """各家错误体 → (类型, 错误码, 说明)。认得 Anthropic、OpenAI、Google 三种写法。"""
    data = _json(body)
    if not isinstance(data, Mapping):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else ""
        return "", "", text[:300]
    err = data.get("error", data)
    if isinstance(err, str):
        return "", "", err[:300]
    if not isinstance(err, Mapping):
        return "", "", ""
    kind = str(err.get("type") or err.get("status") or "")
    code = str(err.get("code") or "")
    message = str(err.get("message") or "")
    return kind, code, message[:300]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    lower = {k.lower(): v for k, v in headers.items()}
    if raw := lower.get("retry-after-ms"):
        try:
            return max(float(raw) / 1000, 0.0)
        except ValueError:
            pass
    raw = lower.get("retry-after")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        try:
            return max(parsedate_to_datetime(raw).timestamp() - time.time(), 0.0)
        except (TypeError, ValueError):
            return None


class ProviderDialect(Protocol):
    spec: ProviderSpec

    def auth(self, fmt: Format, secret: str) -> dict[str, str]: ...

    def url(self, fmt: Format, base: str, op: Op, model: str = "") -> str: ...

    def adjust(self, fmt: Format, body: dict[str, Any], plan: ParamPlan) -> dict[str, Any]: ...

    def usage(self, fmt: Format, raw: Mapping[str, Any]) -> Usage: ...

    def reasoning(self, delta: Mapping[str, Any]) -> str | None: ...

    def classify(
        self,
        status: int | None,
        headers: Mapping[str, str],
        body: bytes | Mapping[str, Any] | None,
    ) -> UpstreamFailure: ...

    async def balance(self, account: AccountRef, client: httpx.AsyncClient) -> Decimal | None: ...

    async def list_models(
        self, account: AccountRef, client: httpx.AsyncClient
    ) -> list[str] | None: ...


class StandardDialect:
    """标准写法：认证、地址按数据；用量、思考按标准字段；报错按状态码和标准错误类型。"""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec

    # ── 认证和地址 ──────────────────────────────────────────────────────

    def auth(self, fmt: Format, secret: str) -> dict[str, str]:
        if not secret:
            return {}
        match self.spec.auth_style(fmt):
            case AuthStyle.X_API_KEY:
                return {"x-api-key": secret, "anthropic-version": ANTHROPIC_VERSION}
            case AuthStyle.API_KEY_HEADER:
                return {"api-key": secret}
            case AuthStyle.GOOG_API_KEY:
                return {"x-goog-api-key": secret}
            case _:
                return {"Authorization": f"Bearer {secret}"}

    def url(self, fmt: Format, base: str, op: Op, model: str = "") -> str:
        path = _PATHS.get((fmt, op))
        if path is None:
            raise ValueError(f"{fmt} 格式没有 {op} 接口")
        return join_url(base, path, model)

    # ── 参数、用量、思考 ────────────────────────────────────────────────

    def adjust(self, fmt: Format, body: dict[str, Any], plan: ParamPlan) -> dict[str, Any]:
        return body

    def usage(self, fmt: Format, raw: Mapping[str, Any]) -> Usage:
        if fmt is Format.ANTHROPIC:
            return Usage(
                input=_int(raw.get("input_tokens")),
                cache_write=_int(raw.get("cache_creation_input_tokens")),
                cache_hit=_int(raw.get("cache_read_input_tokens")),
                output=_int(raw.get("output_tokens")),
            )
        if fmt is Format.OPENAI:
            # 标准写法：输入包含缓存命中的部分，要先减掉
            # 向量、重排有的只给 total_tokens
            prompt = _int(raw.get("prompt_tokens")) or _int(raw.get("total_tokens"))
            cached = _int((raw.get("prompt_tokens_details") or {}).get("cached_tokens"))
            return Usage(
                input=max(prompt - cached, 0),
                cache_hit=cached,
                output=_int(raw.get("completion_tokens")),
            )
        prompt = _int(raw.get("promptTokenCount"))
        cached = _int(raw.get("cachedContentTokenCount"))
        return Usage(
            input=max(prompt - cached, 0),
            cache_hit=cached,
            output=_int(raw.get("candidatesTokenCount")) + _int(raw.get("thoughtsTokenCount")),
        )

    def reasoning(self, delta: Mapping[str, Any]) -> str | None:
        """OpenAI 格式里的思考内容：没有标准字段，常见的是 reasoning_content、reasoning。"""
        value = delta.get("reasoning_content")
        if value is None:
            value = delta.get("reasoning")
        return value if isinstance(value, str) else None

    # ── 报错归类 ────────────────────────────────────────────────────────

    def classify(
        self,
        status: int | None,
        headers: Mapping[str, str],
        body: bytes | Mapping[str, Any] | None,
    ) -> UpstreamFailure:
        kind, code, message = error_fields(body)
        text = f"{status or ''} {kind or code}: {message}".strip(" :")
        failure = self._kind(status, kind.lower(), code.lower())
        return UpstreamFailure(
            kind=failure,
            message=text or "上游出错",
            status=status,
            retry_after_s=_retry_after(headers),
            body=body if failure is FailureKind.BAD_REQUEST and isinstance(body, bytes) else None,
        )

    def _kind(self, status: int | None, kind: str, code: str) -> FailureKind:
        hint = f"{kind} {code}"
        if "billing" in hint or "insufficient_balance" in hint:
            return FailureKind.NO_BALANCE
        if status is None:  # 流里来的错误：只有类型
            if "rate_limit" in hint:
                return FailureKind.RATE_LIMITED
            if "authentication" in hint:
                return FailureKind.AUTH_FAILED
            if "invalid_request" in hint:
                return FailureKind.BAD_REQUEST
            return FailureKind.OVERLOADED
        if status in (400, 413, 422):
            return FailureKind.BAD_REQUEST
        if status == 401:
            return FailureKind.AUTH_FAILED
        if status == 402:
            return FailureKind.NO_BALANCE
        if status == 429:
            return FailureKind.RATE_LIMITED
        # 403 / 404：这个账号用不了这个上游模型（线路配错了），换一条线路试
        return FailureKind.OVERLOADED

    # ── 管理平台用：查余额、拉模型列表 ───────────────────────────────────

    async def balance(self, account: AccountRef, client: httpx.AsyncClient) -> Decimal | None:
        """余额（各家币种不同，原样给出）。None = 这家不支持查余额。"""
        if not (self.spec.balance_url and self.spec.balance_path):
            return None
        resp = await client.get(
            self.spec.balance_url, headers=self.auth(Format.OPENAI, account.secret)
        )
        resp.raise_for_status()
        value = _dig(resp.json(), self.spec.balance_path)
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    async def list_models(self, account: AccountRef, client: httpx.AsyncClient) -> list[str] | None:
        if not self.spec.list_models:
            return None
        fmt = next((f for f in self.spec.formats if account.endpoints.get(f)), None)
        if fmt is None:
            return None
        resp = await client.get(
            self.url(fmt, account.endpoints[fmt], Op.MODELS), headers=self.auth(fmt, account.secret)
        )
        resp.raise_for_status()
        data = resp.json()
        if fmt is Format.GOOGLE:
            return [str(m.get("name", "")).removeprefix("models/") for m in data.get("models", [])]
        return [str(m.get("id")) for m in data.get("data", []) if m.get("id")]
