"""数据面（MCP 方案设计 §5.4、§5.7、§5.8、§8.1、§8.2）：每个 MCP 请求都过这一层 ——
Atlas 的 Native 调用、Pod 里的 CLI 都一样。

    ① 服务     服务存在且启用
    ② 认人     Authorization: Bearer <API Key> → 主体（查库，停用 / 重置立即生效）
    ③ 判权     主体对这个服务有「使用」？（用户中心，短缓存；查不到一律拒绝）
    ④ 复核     tools/call：工具不在册、待复核、被拒绝、不可用 → 拒绝
    ⑤ 改写身份 去掉调用方自带的身份请求头，写入 X-Auth-Uid = Key 认出的主体；
               X-Uid（使用者）调用方带了就照转，只作审计
    ⑥ 转发     主体 Key 换成网关凭据，转给上游；会话标识、推送流、关会话的 DELETE 原样透传
    ⑦ 复核     tools/list：返回里去掉没通过复核的工具 —— 按返回里这版定义的指纹判；
               指纹和库里不一样（上游改了、网关还没刷新）就把这版写进库（registry.observe）
    ⑧ 访问日志 每个请求一行，含被拒的

★ tools/list 读完整返回、改写后再回（要拿到整个列表才能过滤，demo F3）；其余请求流式转发、
  不缓冲（响应可能是 SSE，工具调用可能要几十秒）。
★ 拒绝时回 JSON-RPC 错误体（demo F4）：只回状态码的话，mcp 2.x 客户端只能看到「出错了」。
  error.data 里带 reason（区分得开的原因）和 forwarded（有没有转发给上游）——
  Atlas 靠 forwarded=false 判断能不能安全重试（§6.7），CLI 把 message 交给自己的模型（§7.5）。
★ 只解析需要的消息（§8.2）：方法名、tools/call 的工具名、tools/list 的返回。其余原样透传。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

import anyio
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .auth import use_outbound
from .db.models import McpAccessLog, McpService
from .definitions import normalize
from .errors import Unavailable
from .keys import KeyStore
from .perms import UsableCache
from .registry import Registry
from .settings import GatewaySettings
from .upstream import Upstream, describe_error

logger = logging.getLogger(__name__)

__all__ = ["DataPlane", "Reason"]


class Reason(StrEnum):
    """拒绝原因（error.data.reason）。★ 改名就是改协议：Atlas 按它分支（§6.7）。"""

    SERVICE_NOT_FOUND = "service_not_found"
    SERVICE_DISABLED = "service_disabled"
    KEY_MISSING = "key_missing"
    KEY_INVALID = "key_invalid"
    FORBIDDEN = "forbidden"
    AUTHZ_UNAVAILABLE = "authz_unavailable"
    TOOL_UNKNOWN = "tool_unknown"
    TOOL_PENDING_REVIEW = "tool_pending_review"
    TOOL_REJECTED = "tool_rejected"
    TOOL_INVALID = "tool_invalid"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_REJECTED = "upstream_rejected"


#: 原因 → (HTTP 状态码, JSON-RPC 错误码)。错误码在 JSON-RPC 留给服务端的区间里
_REASONS: dict[Reason, tuple[int, int]] = {
    Reason.SERVICE_NOT_FOUND: (404, -32040),
    Reason.SERVICE_DISABLED: (403, -32041),
    Reason.KEY_MISSING: (401, -32042),
    Reason.KEY_INVALID: (401, -32043),
    Reason.FORBIDDEN: (403, -32044),
    Reason.AUTHZ_UNAVAILABLE: (503, -32045),
    Reason.TOOL_UNKNOWN: (403, -32046),
    Reason.TOOL_PENDING_REVIEW: (403, -32047),
    Reason.TOOL_REJECTED: (403, -32048),
    Reason.TOOL_INVALID: (403, -32049),
    Reason.UPSTREAM_UNAVAILABLE: (502, -32050),
    Reason.UPSTREAM_REJECTED: (502, -32051),
}

_TOOL_REASON = {
    "pending_review": Reason.TOOL_PENDING_REVIEW,
    "rejected": Reason.TOOL_REJECTED,
    "invalid": Reason.TOOL_INVALID,
}

#: 不往上游转的请求头：逐跳的、主体 Key、调用方自带的身份（★ 包括改名前带应用名的旧头）
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
        "cookie",
        "accept-encoding",
        "x-auth-uid",
        "x-uid",
        "x-tenant-id",
        "x-caller-app",
        "x-atlas-user",
    }
)
_DROP_RESPONSE = frozenset(
    {"content-length", "connection", "keep-alive", "transfer-encoding", "content-encoding"}
)


@dataclass
class _Call:
    """一个入站请求里本层关心的东西。"""

    service: str
    http_method: str
    body: bytes
    messages: list[dict[str, Any]]
    batch: bool
    user_uid: str | None
    caller_app: str | None
    run_id: str | None
    started: float = field(default_factory=time.monotonic)
    #: Key 认出的权限主体
    subject_uid: UUID | None = None

    @property
    def subject(self) -> str:
        return str(self.subject_uid) if self.subject_uid else ""

    @property
    def rpc_id(self) -> Any:
        return next((m["id"] for m in self.messages if "id" in m and "method" in m), None)

    @property
    def rpc_method(self) -> str | None:
        methods = [str(m["method"]) for m in self.messages if "method" in m]
        return methods[0] if len(methods) == 1 else (",".join(methods)[:64] or None)

    def calls(self) -> list[str]:
        """这个请求里要调用的工具名（tools/call）。"""
        return [
            str((m.get("params") or {}).get("name") or "")
            for m in self.messages
            if m.get("method") == "tools/call"
        ]

    @property
    def tool(self) -> str | None:
        names = self.calls()
        return names[0] if len(names) == 1 else None

    @property
    def lists_tools(self) -> bool:
        return not self.batch and self.rpc_method == "tools/list"


class _Denied(Exception):
    def __init__(
        self, reason: Reason, *, tool: str | None = None, issues: list[str] | None = None
    ) -> None:
        super().__init__(reason.value)
        self.reason, self.tool, self.issues = reason, tool, issues


def _parse(body: bytes) -> tuple[list[dict[str, Any]], bool]:
    """JSON-RPC 消息（单条或批量）。解析不了的交给上游自己报错。"""
    if not body:
        return [], False
    try:
        parsed = json.loads(body)
    except ValueError:
        return [], False
    if isinstance(parsed, list):
        return [m for m in parsed if isinstance(m, dict)], True
    return ([parsed] if isinstance(parsed, dict) else []), False


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


class _Relay(StreamingResponse):
    """流式转发上游的响应；结束（含客户端断开、取消）时关掉上游连接、写访问日志。"""

    def __init__(
        self,
        upstream: httpx.Response,
        headers: dict[str, str],
        on_close: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers)
        self._upstream = upstream
        self._on_close = on_close

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
        usable: UsableCache,
        http: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.usable = usable
        self.http = http

    async def handle(self, request: Request) -> Response:
        body = await request.body() if request.method == "POST" else b""
        messages, batch = _parse(body)
        h = request.headers
        call = _Call(
            service=request.path_params["service"],
            http_method=request.method,
            body=body,
            messages=messages,
            batch=batch,
            user_uid=(h.get("x-uid") or "").strip() or None,
            caller_app=(h.get("x-caller-app") or "").strip() or None,
            run_id=(h.get("x-run-id") or h.get("x-atlas-run") or "").strip() or None,
        )
        async with self.sessionmaker() as session:
            try:
                svc = await self._admit(session, request, call)
                upstream = Upstream(self.settings, svc.upstream_name)
                try:
                    credential = upstream.credential_headers()
                except RuntimeError as exc:
                    logger.error("MCP 服务 %s 无法转发：%s", call.service, exc)
                    raise _Denied(Reason.UPSTREAM_UNAVAILABLE) from exc
            except _Denied as denied:
                return await self._reject(session, call, denied, forwarded=False)
        return await self._forward(call, upstream.url, credential, request)

    # ------------------------------------------------------------------ ①–④

    async def _admit(self, session: AsyncSession, request: Request, call: _Call) -> McpService:
        """①–④ 都过了 → 服务；否则抛 _Denied。"""
        registry = Registry(session, self.settings)
        svc = await registry.find(call.service)
        if svc is None:
            raise _Denied(Reason.SERVICE_NOT_FOUND)
        if svc.status != "enabled":
            raise _Denied(Reason.SERVICE_DISABLED)
        key = _bearer(request)
        if key is None:
            raise _Denied(Reason.KEY_MISSING)
        holder = await KeyStore(session).authenticate(key)
        if holder is None:
            raise _Denied(Reason.KEY_INVALID)
        call.subject_uid = holder.subject_uid
        # 调用户中心时按主体建立租户上下文（数据面没有入站身份可透传）
        use_outbound({"X-Uid": call.subject, "X-Auth-Uid": call.subject})
        try:
            usable = await self.usable.usable(call.subject)
        except Unavailable as exc:
            raise _Denied(Reason.AUTHZ_UNAVAILABLE) from exc
        if not usable.allows(call.service):
            raise _Denied(Reason.FORBIDDEN)
        for name in call.calls():
            view = await registry.tool(svc, name)
            if view is None:
                raise _Denied(Reason.TOOL_UNKNOWN, tool=name)
            if view.status != "ok":
                raise _Denied(_TOOL_REASON[view.status], tool=name, issues=view.row.issues)
        return svc

    def _rejection(self, call: _Call, denied: _Denied, *, forwarded: bool) -> JSONResponse:
        status, code = _REASONS[denied.reason]
        body = {
            "jsonrpc": "2.0",
            "id": call.rpc_id,
            "error": {
                "code": code,
                "message": _message(denied.reason, call, denied.tool, denied.issues),
                "data": {
                    "reason": denied.reason.value,
                    "forwarded": forwarded,
                    "service": call.service,
                    **({"tool": denied.tool} if denied.tool else {}),
                },
            },
        }
        return JSONResponse(body, status_code=status)

    async def _reject(
        self, session: AsyncSession, call: _Call, denied: _Denied, *, forwarded: bool
    ) -> Response:
        response = self._rejection(call, denied, forwarded=forwarded)
        await self._log(
            session, call, denied.reason.value, response.status_code, forwarded=forwarded
        )
        await session.commit()
        return response

    # ------------------------------------------------------------------ ⑤–⑦

    def _headers(self, request: Request, call: _Call, credential: dict[str, str]) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        headers |= credential
        headers |= {
            "Accept-Encoding": "identity",
            "X-Auth-Uid": call.subject,
            "X-Uid": call.user_uid or call.subject,
            "X-Caller-App": self.settings.uc_app_code,
        }
        return headers

    async def _forward(
        self, call: _Call, url: str, credential: dict[str, str], request: Request
    ) -> Response:
        req = self.http.build_request(
            call.http_method,
            url,
            params=request.query_params,
            content=call.body or None,
            headers=self._headers(request, call, credential),
        )
        try:
            upstream = await self.http.send(req, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("MCP 服务 %s 的上游连不上：%s", call.service, describe_error(exc))
            async with self.sessionmaker() as session:
                return await self._reject(
                    session, call, _Denied(Reason.UPSTREAM_UNAVAILABLE), forwarded=False
                )

        if upstream.status_code in (401, 403):
            # ★ 上游不认网关凭据：配置事故（§8.3）。请求到了上游但没有执行
            await upstream.aclose()
            logger.error(
                "MCP 服务 %s 的上游拒绝了网关凭据（HTTP %s）", call.service, upstream.status_code
            )
            async with self.sessionmaker() as session:
                return await self._reject(
                    session, call, _Denied(Reason.UPSTREAM_REJECTED), forwarded=True
                )

        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
        if call.lists_tools and upstream.status_code == 200:
            try:
                content = await upstream.aread()
            finally:
                await upstream.aclose()
            content = await self._filter_tools(call, content, headers.get("content-type", ""))
            async with self.sessionmaker() as session:
                await self._log(session, call, "ok", upstream.status_code, forwarded=True)
                await session.commit()
            return Response(content, status_code=upstream.status_code, headers=headers)

        async def done() -> None:
            async with self.sessionmaker() as session:
                await self._log(session, call, "ok", upstream.status_code, forwarded=True)
                await session.commit()

        return _Relay(upstream, headers, done)

    async def _filter_tools(self, call: _Call, content: bytes, content_type: str) -> bytes:
        """tools/list 的返回里只留复核通过、而且就是复核过的那一版定义的工具。"""
        if content_type.partition(";")[0].strip().lower() == "text/event-stream":
            return await _rewrite_sse(content, lambda m: self._filter_message(call, m))
        try:
            message = json.loads(content)
        except ValueError:
            return content
        if not isinstance(message, dict):
            return content
        return json.dumps(await self._filter_message(call, message), ensure_ascii=False).encode()

    async def _filter_message(self, call: _Call, message: dict[str, Any]) -> dict[str, Any]:
        result = message.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return message
        defs = [normalize(call.service, t) if isinstance(t, dict) else None for t in tools]
        async with self.sessionmaker() as session:
            registry = Registry(session, self.settings)
            svc = await registry.get(call.service)
            views = {v.row.name: v for v in await registry.tools(svc)}
            drifted = [
                t
                for t, d in zip(tools, defs, strict=True)
                if d is not None and ((v := views.get(d.name)) is None or v.row.digest != d.digest)
            ]
            if drifted:
                changed = await registry.observe(svc, drifted)
                await session.commit()
                logger.warning(
                    "MCP 服务 %s 的 tools/list 里定义变了，已按这版记录：%s",
                    call.service,
                    "、".join(changed),
                )
                views = {v.row.name: v for v in await registry.tools(svc)}
        kept = [
            t
            for t, d in zip(tools, defs, strict=True)
            if d is not None
            and (v := views.get(d.name)) is not None
            and v.status == "ok"
            and v.row.digest == d.digest
        ]
        if len(kept) != len(tools):
            logger.info(
                "MCP 服务 %s 的 tools/list 去掉了 %d 个未通过复核的工具",
                call.service,
                len(tools) - len(kept),
            )
        return {**message, "result": {**result, "tools": kept}}

    # ------------------------------------------------------------------ ⑧

    async def _log(
        self, session: AsyncSession, call: _Call, outcome: str, status: int, *, forwarded: bool
    ) -> None:
        """★ 写日志失败不能让调用失败：只记一条告警。"""
        try:
            session.add(
                McpAccessLog(
                    service=call.service[:64],
                    subject_uid=call.subject_uid,
                    user_uid=(call.user_uid or "")[:64] or None,
                    caller_app=(call.caller_app or "")[:64] or None,
                    run_id=(call.run_id or "")[:64] or None,
                    http_method=call.http_method[:8],
                    rpc_method=call.rpc_method,
                    tool=(call.tool or "")[:128] or None,
                    outcome=outcome,
                    status=status,
                    forwarded=forwarded,
                    duration_ms=int((time.monotonic() - call.started) * 1000),
                )
            )
            await session.flush()
        except Exception:
            logger.warning("写 MCP 访问日志失败", exc_info=True)
            await session.rollback()


def _message(reason: Reason, call: _Call, tool: str | None, issues: list[str] | None) -> str:
    service = call.service
    match reason:
        case Reason.SERVICE_NOT_FOUND:
            return f"MCP 服务 {service} 不存在"
        case Reason.SERVICE_DISABLED:
            return f"MCP 服务 {service} 已被停用"
        case Reason.KEY_MISSING:
            return "缺少 API Key：请求头要带 Authorization: Bearer <MCP 网关发放的 Key>"
        case Reason.KEY_INVALID:
            return "API Key 无效或已停用，请联系 MCP 管理员"
        case Reason.FORBIDDEN:
            return (
                f"{call.subject} 没有被授权使用 MCP 服务 {service}，请联系服务所有者或 MCP 管理员"
            )
        case Reason.AUTHZ_UNAVAILABLE:
            return f"暂时无法确认 MCP 服务 {service} 的授权（用户中心不可用），请稍后重试"
        case Reason.TOOL_UNKNOWN:
            return f"MCP 服务 {service} 上没有工具 {tool}"
        case Reason.TOOL_PENDING_REVIEW:
            return f"工具 {tool} 的定义待复核，暂时不能调用"
        case Reason.TOOL_REJECTED:
            return f"工具 {tool} 的定义没有通过复核，不能调用"
        case Reason.TOOL_INVALID:
            return f"工具 {tool} 的定义不可用：{'；'.join(issues or [])}"
        case Reason.UPSTREAM_UNAVAILABLE:
            return f"MCP 服务 {service} 暂时不可用，请稍后重试"
        case Reason.UPSTREAM_REJECTED:
            return f"MCP 服务 {service} 暂时不可用（网关配置问题），请联系 MCP 管理员"


async def _rewrite_sse(
    content: bytes, rewrite: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
) -> bytes:
    """SSE 返回：改写每个事件里 data 的 JSON-RPC 消息，其余行原样保留。"""
    text = content.decode("utf-8")
    out: list[str] = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        if not event.strip():
            continue
        lines = [(ln, *ln.partition(":")[::2]) for ln in event.split("\n")]
        data = "\n".join(value.lstrip() for _, name, value in lines if name == "data")
        try:
            message = json.loads(data) if data else None
        except ValueError:
            message = None
        if not isinstance(message, dict):
            out.append(event)
            continue
        others = [ln for ln, name, _ in lines if name != "data"]
        rewritten = json.dumps(await rewrite(message), ensure_ascii=False)
        out.append("\n".join([*others, f"data: {rewritten}"]))
    return ("\n\n".join(out) + "\n\n").encode()
