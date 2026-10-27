"""数据面（模型管理方案设计 §6.4、§7.2、§9.2）：每个模型请求都过这一层 —— Atlas 的 Native 调用、
Pod 里的 CLI、平台功能都一样。

    ① 解析     路径 → 端点（类型、协议）；请求体里的模型名（只解析这一处）
    ② 认人     X-Model-Purpose（平台功能：应用 + 用途）优先；否则主体 Key（Authorization: Bearer
               或 x-api-key）→ 主体（查库，停用 / 重置立即生效）
    ③ 模型     存在、类型和协议对得上、没停用、不在接入中、没下线
    ④ 判权     主体：对话模型要有「使用」（用户中心，短缓存，不可用时沿用上一次的结果）；
               嵌入、重排只供平台功能。平台用途：模型要在这个用途的允许范围里
    ⑤ 转发     平台模型名换成上游模型名；OpenAI 兼容的流式请求补「返回用量」；去掉主体 Key 和
               身份请求头（不把内部身份发给外部供应商），换上网关凭据；流式转发，不缓冲
    ⑥ 计量     边转发边读用量；结束（含客户端断开、取消）时异步写用量记录，含被拒的

★ 拒绝按调用协议的错误格式回（Anthropic / OpenAI），error 里带 reason（区分得开的原因）和
  forwarded（有没有转发给上游）—— Atlas 靠 reason 区分「找人授权」和「稍后再试」（§7.3）。
★ 只改两处请求内容（§9.2）：提示词和参数原样转发，提示词缓存不受影响。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import anyio
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .auth import use_outbound
from .catalog import PURPOSES
from .db.models import GwModel, GwPurpose
from .db.types import utcnow
from .errors import Unavailable
from .keys import KeyStore
from .perms import SubjectsCache
from .settings import GatewaySettings
from .upstream import Upstream, describe_error
from .usage import Meter, Recorder, Usage

logger = logging.getLogger(__name__)

__all__ = ["ENDPOINTS", "DataPlane", "Reason"]


class Reason(StrEnum):
    """拒绝原因（error.reason）。★ 改名就是改协议：Atlas 按它分支（§7.3）。"""

    INVALID_REQUEST = "invalid_request"
    KEY_MISSING = "key_missing"
    KEY_INVALID = "key_invalid"
    CALLER_MISSING = "caller_missing"
    PURPOSE_UNKNOWN = "purpose_unknown"
    PURPOSE_MODEL_NOT_ALLOWED = "purpose_model_not_allowed"
    FORBIDDEN = "forbidden"
    PLATFORM_ONLY = "platform_only"
    AUTHZ_UNAVAILABLE = "authz_unavailable"
    MODEL_NOT_FOUND = "model_not_found"
    KIND_MISMATCH = "kind_mismatch"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    MODEL_DISABLED = "model_disabled"
    MODEL_ONBOARDING = "model_onboarding"
    MODEL_RETIRED = "model_retired"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_REJECTED = "upstream_rejected"


#: 原因 → (HTTP 状态码, Anthropic 的错误类型, OpenAI 的错误类型)
#: ★ 暂时性的用 5xx：两家 SDK 都会自动退避重试（§7.3 ⑤）；权限和状态问题用 4xx，不重试
_REASONS: dict[Reason, tuple[int, str, str]] = {
    Reason.INVALID_REQUEST: (400, "invalid_request_error", "invalid_request_error"),
    Reason.KEY_MISSING: (401, "authentication_error", "authentication_error"),
    Reason.KEY_INVALID: (401, "authentication_error", "authentication_error"),
    Reason.CALLER_MISSING: (401, "authentication_error", "authentication_error"),
    Reason.PURPOSE_UNKNOWN: (400, "invalid_request_error", "invalid_request_error"),
    Reason.PURPOSE_MODEL_NOT_ALLOWED: (403, "permission_error", "permission_error"),
    Reason.FORBIDDEN: (403, "permission_error", "permission_error"),
    Reason.PLATFORM_ONLY: (403, "permission_error", "permission_error"),
    Reason.AUTHZ_UNAVAILABLE: (503, "api_error", "server_error"),
    Reason.MODEL_NOT_FOUND: (404, "not_found_error", "invalid_request_error"),
    Reason.KIND_MISMATCH: (400, "invalid_request_error", "invalid_request_error"),
    Reason.PROTOCOL_UNSUPPORTED: (400, "invalid_request_error", "invalid_request_error"),
    Reason.MODEL_DISABLED: (403, "permission_error", "permission_error"),
    Reason.MODEL_ONBOARDING: (403, "permission_error", "permission_error"),
    Reason.MODEL_RETIRED: (403, "permission_error", "permission_error"),
    Reason.UPSTREAM_UNAVAILABLE: (502, "api_error", "server_error"),
    Reason.UPSTREAM_REJECTED: (502, "api_error", "server_error"),
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str
    protocol: str
    path: str
    #: 计量（count_tokens 只数 token，不产生用量）
    metered: bool = True


ENDPOINTS: dict[str, Endpoint] = {
    e.path: e
    for e in (
        Endpoint("messages", "chat", "anthropic", "/v1/messages"),
        Endpoint("count_tokens", "chat", "anthropic", "/v1/messages/count_tokens", metered=False),
        Endpoint("chat", "chat", "openai", "/v1/chat/completions"),
        Endpoint("embeddings", "embedding", "openai", "/v1/embeddings"),
        Endpoint("rerank", "rerank", "rerank", "/v1/rerank"),
    )
}

#: 不往上游转的请求头：逐跳的、主体 Key、调用方自带的身份与归属（不把内部身份发给外部供应商）
_DROP_REQUEST = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
        "authorization",
        "x-api-key",
        "cookie",
        "accept-encoding",
        "x-auth-uid",
        "x-uid",
        "x-tenant-id",
        "x-caller-app",
        "x-model-purpose",
        "x-agent-id",
        "x-run-id",
        "x-thread-id",
        "x-atlas-user",
        "x-atlas-run",
    }
)
_DROP_RESPONSE = frozenset(
    {"content-length", "connection", "keep-alive", "transfer-encoding", "content-encoding"}
)


@dataclass
class _Call:
    """一个入站请求里本层关心的东西。"""

    endpoint: Endpoint
    body: bytes
    payload: dict[str, Any] | None
    caller_app: str | None
    user_uid: str | None
    agent_id: str | None
    run_id: str | None
    purpose: str | None
    started: float = field(default_factory=time.monotonic)
    at: datetime = field(default_factory=utcnow)
    #: Key 认出的权限主体
    subject_uid: UUID | None = None
    dept_id: str | None = None
    model: GwModel | None = None
    ttft_ms: int | None = None

    @property
    def model_name(self) -> str:
        value = (self.payload or {}).get("model")
        return value.strip() if isinstance(value, str) else ""

    @property
    def stream(self) -> bool:
        return bool((self.payload or {}).get("stream"))

    def row(self, outcome: str, status: int, *, forwarded: bool) -> dict[str, Any]:
        """用量记录里除了用量以外的字段。"""
        return {
            "model": self.model_name[:64],
            "upstream_model": self.model.upstream_model if self.model is not None else None,
            "endpoint": self.endpoint.name,
            "stream": self.stream,
            "subject_uid": self.subject_uid,
            "dept_id": self.dept_id,
            "purpose": self.purpose,
            "caller_app": (self.caller_app or "")[:64] or None,
            "user_uid": (self.user_uid or "")[:64] or None,
            "agent_id": (self.agent_id or "")[:64] or None,
            "run_id": (self.run_id or "")[:64] or None,
            "outcome": outcome,
            "status": status,
            "forwarded": forwarded,
            "ttft_ms": self.ttft_ms,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
        }


class _Denied(Exception):
    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason, self.message = reason, message


def _key(request: Request) -> str | None:
    """主体 Key：Authorization: Bearer（OpenAI 兼容、Claude Code）或 x-api-key（Anthropic SDK）。"""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.headers.get("x-api-key") or "").strip() or None


def _header(request: Request, name: str) -> str | None:
    return (request.headers.get(name) or "").strip() or None


class _Relay(StreamingResponse):
    """流式转发上游的响应：边转发边喂给计量；结束（含客户端断开、取消）时关掉上游连接、写记录。"""

    def __init__(
        self,
        upstream: httpx.Response,
        headers: dict[str, str],
        call: _Call,
        meter: Meter | None,
        on_close: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(
            self._relay(upstream, call, meter), status_code=upstream.status_code, headers=headers
        )
        self._upstream = upstream
        self._on_close = on_close

    @staticmethod
    async def _relay(upstream: httpx.Response, call: _Call, meter: Meter | None) -> Any:
        async for chunk in upstream.aiter_raw():
            if call.ttft_ms is None:
                call.ttft_ms = int((time.monotonic() - call.started) * 1000)
            if meter is not None:
                meter.feed(chunk)
            yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._upstream.aclose()
                await self._on_close()


class DataPlane:
    def __init__(
        self,
        settings: GatewaySettings,
        sessionmaker: async_sessionmaker[AsyncSession],
        subjects: SubjectsCache,
        http: httpx.AsyncClient,
        recorder: Recorder,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.subjects = subjects
        self.http = http
        self.recorder = recorder
        self.upstream = Upstream(settings)

    # ------------------------------------------------------------------ 入口

    async def handle(self, request: Request, endpoint: Endpoint) -> Response:
        body = await request.body()
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
        call = _Call(
            endpoint=endpoint,
            body=body,
            payload=payload if isinstance(payload, dict) else None,
            caller_app=_header(request, "x-caller-app"),
            user_uid=_header(request, "x-uid"),
            agent_id=_header(request, "x-agent-id"),
            run_id=_header(request, "x-run-id"),
            purpose=_header(request, "x-model-purpose"),
        )
        if len(body) > self.settings.max_body_bytes:
            return await self._reject(call, _Denied(Reason.INVALID_REQUEST, "请求体太大"))
        try:
            async with self.sessionmaker() as session:
                await self._admit(session, request, call)
            credential = self.upstream.credential_headers()
        except _Denied as denied:
            return await self._reject(call, denied)
        except RuntimeError as exc:
            logger.error("模型 %s 无法转发：%s", call.model_name, exc)
            return await self._reject(
                call, _Denied(Reason.UPSTREAM_UNAVAILABLE, f"模型 {call.model_name} 暂时不可用")
            )
        return await self._forward(call, request, credential)

    async def models(self, request: Request) -> Response:
        """GET /v1/models：主体能用的对话模型，或这个用途允许的模型。

        OpenAI 与 Anthropic 两种列表格式的字段都给。
        """
        call = _Call(
            endpoint=Endpoint("models", "chat", "openai", "/v1/models", metered=False),
            body=b"",
            payload=None,
            caller_app=_header(request, "x-caller-app"),
            user_uid=_header(request, "x-uid"),
            agent_id=_header(request, "x-agent-id"),
            run_id=_header(request, "x-run-id"),
            purpose=_header(request, "x-model-purpose"),
        )
        try:
            async with self.sessionmaker() as session:
                purpose = await self._identify(session, request, call)
                rows = list(await session.scalars(select(GwModel).order_by(GwModel.name)))
                visible = [
                    r for r in rows if r.status in ("available", "deprecated") and not r.disabled
                ]
                if purpose is not None:
                    allowed = set(_allowed(purpose))
                    visible = [r for r in visible if r.name in allowed]
                else:
                    usable = await self._usable(call)
                    visible = [r for r in visible if r.kind == "chat" and usable.allows(r.name)]
        except _Denied as denied:
            return await self._reject(call, denied)
        data = [
            {
                "id": r.name,
                "object": "model",
                "type": "model",
                "display_name": r.display_name,
                "owned_by": r.provider,
                "created_at": r.created_at.isoformat(),
            }
            for r in visible
        ]
        await self.recorder.record(
            call.row("ok", 200, forwarded=False), model_uuid=None, usage=None, at=call.at
        )
        return JSONResponse(
            {
                "object": "list",
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    # ------------------------------------------------------------------ ②–④

    async def _identify(
        self, session: AsyncSession, request: Request, call: _Call
    ) -> GwPurpose | None:
        """② 认人。→ 平台用途的配置（平台功能）；主体 Key 时为 None（主体记在 call 上）。"""
        if call.purpose is not None:
            purpose = PURPOSES.get(call.purpose)
            if purpose is None or not purpose.callable:
                raise _Denied(Reason.PURPOSE_UNKNOWN, f"平台用途 {call.purpose} 不存在")
            if call.caller_app is None:
                raise _Denied(
                    Reason.CALLER_MISSING, "平台用途的调用要带上调用方应用（X-Caller-App）"
                )
            row = await session.scalar(select(GwPurpose).where(GwPurpose.code == call.purpose))
            return row or GwPurpose(code=call.purpose, default_model=None, allowed_models=[])
        key = _key(request)
        if key is None:
            raise _Denied(
                Reason.KEY_MISSING,
                "缺少 Key：请求头要带 Authorization: Bearer <模型网关的 Key> 或 x-api-key",
            )
        holder = await KeyStore(session).authenticate(key)
        if holder is None:
            raise _Denied(
                Reason.KEY_INVALID, "Key 无效或已停用：可能刚被重置，请重试或联系模型管理员"
            )
        call.subject_uid = holder.subject_uid
        # 调用户中心时按主体建立租户上下文（数据面没有入站身份可透传）
        use_outbound({"X-Uid": str(holder.subject_uid), "X-Auth-Uid": str(holder.subject_uid)})
        return None

    async def _usable(self, call: _Call) -> Any:
        assert call.subject_uid is not None
        try:
            usable = await self.subjects.usable(str(call.subject_uid))
        except Unavailable as exc:
            raise _Denied(
                Reason.AUTHZ_UNAVAILABLE, "暂时无法确认模型的授权（用户中心不可用），请稍后重试"
            ) from exc
        try:
            info = await self.subjects.info(str(call.subject_uid))
            call.dept_id = info.dept_id if info is not None else None
        except Unavailable:
            call.dept_id = None
        return usable

    async def _admit(self, session: AsyncSession, request: Request, call: _Call) -> None:
        """②–④ 都过了就返回；否则抛 _Denied。"""
        endpoint = call.endpoint
        if call.payload is None or not call.model_name:
            raise _Denied(Reason.INVALID_REQUEST, "请求体要是 JSON，并且带上 model")
        purpose = await self._identify(session, request, call)
        model = await session.scalar(select(GwModel).where(GwModel.name == call.model_name))
        if model is None:
            raise _Denied(Reason.MODEL_NOT_FOUND, f"模型 {call.model_name} 不存在")
        call.model = model
        if model.kind != endpoint.kind:
            raise _Denied(
                Reason.KIND_MISMATCH, f"模型 {model.name} 是 {model.kind} 模型，不能这样调用"
            )
        if endpoint.protocol not in (model.protocols or []):
            raise _Denied(
                Reason.PROTOCOL_UNSUPPORTED,
                f"模型 {model.name} 不支持这种调用协议（{endpoint.protocol}），"
                f"支持：{', '.join(model.protocols or []) or '无'}",
            )
        if model.disabled:
            reason = f"：{model.disabled_reason}" if model.disabled_reason else ""
            raise _Denied(Reason.MODEL_DISABLED, f"模型 {model.name} 已被停用{reason}")
        if model.status == "onboarding":
            raise _Denied(Reason.MODEL_ONBOARDING, f"模型 {model.name} 还在接入中，暂不能调用")
        if model.status == "retired":
            hint = f"，请改用 {model.replacement}" if model.replacement else ""
            raise _Denied(Reason.MODEL_RETIRED, f"模型 {model.name} 已下线{hint}")
        if purpose is not None:
            if model.name not in _allowed(purpose):
                raise _Denied(
                    Reason.PURPOSE_MODEL_NOT_ALLOWED,
                    f"模型 {model.name} 不在平台用途「{PURPOSES[purpose.code].name}」的允许范围里",
                )
            return
        if model.kind != "chat":
            raise _Denied(Reason.PLATFORM_ONLY, f"{model.kind} 模型只供平台功能使用")
        usable = await self._usable(call)
        if not usable.allows(model.name):
            raise _Denied(
                Reason.FORBIDDEN,
                f"{call.subject_uid} 没有被授权使用模型 {model.name}，请联系模型所有者或模型管理员",
            )

    # ------------------------------------------------------------------ ⑤–⑥

    def _request_body(self, call: _Call) -> bytes:
        """只改两处：模型名换成上游模型名；OpenAI 兼容的流式请求补「返回用量」。没改就原样转发。"""
        assert call.model is not None and call.payload is not None
        payload = call.payload
        changed = False
        if call.model.upstream_model != call.model_name:
            payload = {**payload, "model": call.model.upstream_model}
            changed = True
        if call.endpoint.name == "chat" and call.stream:
            options = payload.get("stream_options")
            if not (isinstance(options, dict) and options.get("include_usage")):
                payload = {
                    **payload,
                    "stream_options": {
                        **(options if isinstance(options, dict) else {}),
                        "include_usage": True,
                    },
                }
                changed = True
        return json.dumps(payload, ensure_ascii=False).encode() if changed else call.body

    def _headers(self, request: Request, credential: dict[str, str]) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        return headers | credential | {"Accept-Encoding": "identity"}

    async def _forward(self, call: _Call, request: Request, credential: dict[str, str]) -> Response:
        assert call.model is not None
        req = self.http.build_request(
            "POST",
            self.upstream.url(call.endpoint.path),
            params=request.query_params,
            content=self._request_body(call),
            headers=self._headers(request, credential),
        )
        try:
            upstream = await self.http.send(req, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("模型 %s 的上游连不上：%s", call.model_name, describe_error(exc))
            return await self._reject(
                call,
                _Denied(
                    Reason.UPSTREAM_UNAVAILABLE, f"模型 {call.model_name} 暂时不可用，请稍后重试"
                ),
            )
        if upstream.status_code in (401, 403):
            # ★ 上游不认网关凭据：配置事故（§9.3）。请求到了上游但没有执行
            await upstream.aclose()
            logger.error(
                "模型 %s 的上游拒绝了网关凭据（HTTP %s）", call.model_name, upstream.status_code
            )
            return await self._reject(
                call,
                _Denied(
                    Reason.UPSTREAM_REJECTED,
                    f"模型 {call.model_name} 暂时不可用（网关配置问题），请联系模型管理员",
                ),
                forwarded=True,
            )
        if upstream.status_code == 402:
            logger.error(
                "模型 %s 的供应商拒绝了请求（HTTP 402，额度可能已经用完）", call.model_name
            )
        elif upstream.status_code >= 500:
            logger.warning("模型 %s 的上游返回 HTTP %s", call.model_name, upstream.status_code)

        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
        meter = (
            Meter(call.endpoint.name, limit=self.settings.max_meter_bytes)
            if call.endpoint.metered and upstream.status_code < 400
            else None
        )
        if meter is not None:
            meter.start(upstream.headers.get("content-type", ""))
        outcome = "ok" if upstream.status_code < 400 else "upstream_error"
        model_uuid = call.model.uuid

        async def done() -> None:
            usage: Usage | None = meter.result() if meter is not None else None
            await self.recorder.record(
                call.row(outcome, upstream.status_code, forwarded=True),
                model_uuid=model_uuid,
                usage=usage,
                at=call.at,
            )

        return _Relay(upstream, headers, call, meter, done)

    async def _reject(self, call: _Call, denied: _Denied, *, forwarded: bool = False) -> Response:
        status, anthropic_type, openai_type = _REASONS[denied.reason]
        extra = {"reason": denied.reason.value, "forwarded": forwarded}
        if call.endpoint.protocol == "anthropic":
            body: dict[str, Any] = {
                "type": "error",
                "error": {"type": anthropic_type, "message": denied.message, **extra},
            }
        else:
            body = {
                "error": {
                    "message": denied.message,
                    "type": openai_type,
                    "code": denied.reason.value,
                    **extra,
                }
            }
        await self.recorder.record(
            call.row(denied.reason.value, status, forwarded=forwarded),
            model_uuid=None,
            usage=None,
            at=call.at,
        )
        return JSONResponse(body, status_code=status)


def _allowed(purpose: GwPurpose) -> list[str]:
    return [
        *([purpose.default_model] if purpose.default_model else []),
        *(purpose.allowed_models or []),
    ]
