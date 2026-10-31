"""数据面：网关自己就是 MCP 服务端（MCP SDK 的 lowlevel Server，无状态、JSON 响应）。

    /mcp/{服务名}   POST（JSON-RPC）。GET、DELETE 回 405：无状态，没有推送流、没有会话可关

    ① 服务        存在且启用                                  -32040 / -32041
    ② 认 Key      Authorization: Bearer <Key>（查库，停用、重置立即生效）  -32042 / -32043
    ③ 判权        公开服务不判；私有服务以 Key 的主体 uid 问用户中心（短缓存），
                  这把 Key 在这个服务上一点权限都没有就拒绝          -32044 / -32045
                  ①–③ 在交给 SDK 之前做：回 HTTP 401 / 403 / 404 / 503 + JSON-RPC 错误体
    ④ tools/list  按库里的定义生成，只列这把 Key 能用的；不转发给上游
    ⑤ tools/call  工具存在且可用 -32046；工具级判权 -32044；按参数定义查必填和类型 -32602；
                  按服务类型转换后调上游：连不上 -32050，上游回了 JSON-RPC 错误 -32051
    ⑥ 身份        上游收到 X-Auth-Uid = Key 的主体 uid、X-Uid = 使用者（用户 Key 是持有人，
                  智能体 Key 是调用方带来的 X-Uid），以及调用方的 X-Run-Id、X-Thread-Id；
                  调用方的 Key 和别的请求头不转发

★ 无状态：每个请求独立，任一实例都能处理；不发会话标识。上游 MCP 的会话在一次调用里开关。
★ 只对外暴露工具：resources、prompts 不转发。
★ 拒绝回 JSON-RPC 错误体，error.data 带 reason 和 forwarded（有没有转发给上游）—— Atlas 按 reason
  分支，靠 forwarded=false 判断能不能安全重试。★ reason 改名就是改协议。
★ 管理端「试调」走同一段逻辑（execute），判权、校验、错误码都一样。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from . import tracing
from .auth import use_outbound
from .db.models import McpApiKey, McpService, McpTool
from .errors import Unavailable
from .keys import KeyService
from .perms import AccessCache, KeyAccess, Permissions
from .upstreams import CallContext, UpstreamError, UpstreamUnavailable
from .upstreams.grpc import InvalidArguments
from .upstreams.hub import Upstreams

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("atlas_mcpgateway")

__all__ = ["Caller", "DataPlane", "Reason", "Rejection"]


class Reason(StrEnum):
    """拒绝原因（error.data.reason）。★ 改名就是改协议：Atlas 按它分支。"""

    SERVICE_NOT_FOUND = "service_not_found"
    SERVICE_DISABLED = "service_disabled"
    KEY_MISSING = "key_missing"
    KEY_INVALID = "key_invalid"
    FORBIDDEN = "forbidden"
    AUTHZ_UNAVAILABLE = "authz_unavailable"
    TOOL_UNKNOWN = "tool_unknown"
    INVALID_PARAMS = "invalid_params"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"


#: 原因 → (HTTP 状态码, JSON-RPC 错误码)。错误码在 JSON-RPC 留给服务端的区间里（-32602 是标准的）
_CODES: dict[Reason, tuple[int, int]] = {
    Reason.SERVICE_NOT_FOUND: (404, -32040),
    Reason.SERVICE_DISABLED: (403, -32041),
    Reason.KEY_MISSING: (401, -32042),
    Reason.KEY_INVALID: (401, -32043),
    Reason.FORBIDDEN: (403, -32044),
    Reason.AUTHZ_UNAVAILABLE: (503, -32045),
    Reason.TOOL_UNKNOWN: (200, -32046),
    Reason.INVALID_PARAMS: (200, -32602),
    Reason.UPSTREAM_UNAVAILABLE: (200, -32050),
    Reason.UPSTREAM_ERROR: (200, -32051),
}


class Rejection(Exception):
    def __init__(
        self, reason: Reason, message: str, *, forwarded: bool = False, **extra: Any
    ) -> None:
        super().__init__(message)
        self.reason, self.message, self.forwarded, self.extra = reason, message, forwarded, extra

    @property
    def code(self) -> int:
        return _CODES[self.reason][1]

    @property
    def http_status(self) -> int:
        return _CODES[self.reason][0]

    def error(self, service: str) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "data": {
                "reason": self.reason.value,
                "forwarded": self.forwarded,
                "service": service,
                **self.extra,
            },
        }


@dataclass
class Caller:
    """认过的一次调用：服务、Key、这把 Key 在私有服务上的权限（公开服务为 None）。"""

    service: McpService
    key: McpApiKey
    access: KeyAccess | None
    #: 使用者（只作审计）：用户 Key 是持有人；智能体 Key 是调用方带来的 X-Uid
    user_uid: str | None
    #: 调用方带来的审计头（X-Run-Id、X-Thread-Id）
    audit: dict[str, str] = field(default_factory=dict)

    def allows(self, tool: str) -> bool:
        return self.access is None or self.access.allows(self.service.name, tool)

    def context(self) -> CallContext:
        return CallContext(
            key_uid=str(self.key.subject_uid),
            user_uid=self.user_uid,
            pass_identity=self.service.pass_identity,
            audit=self.audit,
        )


#: 调用方的审计头：照转给上游（开着「传递调用者身份」时）。别的请求头一律不转
_AUDIT_HEADERS = ("X-Run-Id", "X-Thread-Id")


def _bearer(headers: Mapping[str, str]) -> str | None:
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _first(body: bytes) -> dict[str, Any] | None:
    """请求体里的（第一条）JSON-RPC 消息。解析不了交给 SDK 自己报错。"""
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        return None
    if isinstance(parsed, list):
        parsed = next((m for m in parsed if isinstance(m, dict) and "id" in m), None)
    return parsed if isinstance(parsed, dict) else None


def _rpc_id(message: dict[str, Any] | None) -> Any:
    return message.get("id") if message is not None else None


def _meta(message: dict[str, Any] | None) -> dict[str, Any] | None:
    params = message.get("params") if message is not None else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta if isinstance(meta, dict) else None


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def check_arguments(schema: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    """按参数定义查必填和顶层类型（不做严格模式：多给的参数不拒绝）。→ 问题说明，没问题为 None。"""
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name in schema.get("required") or []:
        if name not in args or args[name] is None:
            return f"缺少必填参数 {name}"
    for name, value in args.items():
        prop = props.get(name)
        expected = prop.get("type") if isinstance(prop, dict) else None
        if not isinstance(expected, str) or expected not in _JSON_TYPES or value is None:
            continue
        ok = isinstance(value, _JSON_TYPES[expected])
        if expected in ("integer", "number") and isinstance(value, bool):
            ok = False
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            ok = True
        if not ok:
            return f"参数 {name} 应该是 {expected}"
    return None


def callable_tool(tool: McpTool) -> bool:
    return tool.status == "active" and tool.removed_at is None and not tool.issues


class DataPlane:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        perms: Permissions,
        access: AccessCache,
        upstreams: Upstreams,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.perms = perms
        self.access = access
        self.upstreams = upstreams
        server: Server[Any, Any] = Server(
            "mcp-gateway", on_list_tools=self._list_tools, on_call_tool=self._call_tool
        )
        self.manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """起 SDK 的会话管理器。

        ★ 进入、退出它的 task group 必须在同一个 task 里：放进自己的 task。
        """
        started = asyncio.Event()

        async def run() -> None:
            async with self.manager.run():
                started.set()
                await self._stop.wait()

        self._task = asyncio.create_task(run())
        waiter = asyncio.create_task(started.wait())
        done, _ = await asyncio.wait({self._task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if self._task in done:
            waiter.cancel()
            await self._task  # 起不来：把异常抛出去

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    # ------------------------------------------------------------------ ①–③

    async def admit(
        self,
        service: str,
        secret: str | None,
        *,
        user_uid: str | None,
        audit: Mapping[str, str] | None = None,
    ) -> Caller:
        async with self.sessionmaker() as session:
            svc = await session.scalar(select(McpService).where(McpService.name == service))
            if svc is None:
                raise Rejection(Reason.SERVICE_NOT_FOUND, f"MCP 服务 {service} 不存在")
            if svc.status != "enabled":
                raise Rejection(Reason.SERVICE_DISABLED, f"MCP 服务 {service} 已被停用")
            if secret is None:
                raise Rejection(
                    Reason.KEY_MISSING,
                    "缺少 API Key：请求头要带 Authorization: Bearer <MCP 网关的 API Key>",
                )
            key = await KeyService(session, self.perms).authenticate(secret)
            if key is None:
                raise Rejection(Reason.KEY_INVALID, "API Key 无效或已停用")
        tracing.mark(service=service, key_uid=str(key.subject_uid), key_type=key.key_type)
        access: KeyAccess | None = None
        if svc.visibility == "private":
            # ★ 调用户中心判权时 X-Auth-Uid = Key 的主体：数据面没有别的身份可透传
            subject = str(key.subject_uid)
            use_outbound({"X-Uid": subject, "X-Auth-Uid": subject})
            try:
                access = await self.access.get(str(key.subject_uid))
            except Unavailable as exc:
                raise Rejection(
                    Reason.AUTHZ_UNAVAILABLE,
                    f"暂时无法确认 MCP 服务 {service} 的授权（用户中心不可用），请稍后重试",
                ) from exc
            if not access.touches(service):
                raise Rejection(
                    Reason.FORBIDDEN,
                    f"这把 API Key 没有被授权使用 MCP 服务 {service}，"
                    "请联系服务所有者或 MCP 管理员",
                    key_uid=str(key.subject_uid),
                )
        holder = str(key.user_uuid) if key.key_type == "user" else (user_uid or None)
        return Caller(svc, key, access, holder, dict(audit or {}))

    # ------------------------------------------------------------------ ④⑤

    async def tools(self, svc: McpService) -> list[McpTool]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(McpTool).where(McpTool.service_uuid == svc.uuid).order_by(McpTool.name)
            )
            return [t for t in rows if callable_tool(t)]

    async def visible(self, caller: Caller) -> list[McpTool]:
        return [t for t in await self.tools(caller.service) if caller.allows(t.name)]

    async def execute(
        self, caller: Caller, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """⑤：→ MCP 线上格式的 CallToolResult；拒绝抛 Rejection。"""
        svc = caller.service
        tracing.mark(tool=name)
        tool = next((t for t in await self.tools(svc) if t.name == name), None)
        if tool is None:
            raise Rejection(
                Reason.TOOL_UNKNOWN, f"MCP 服务 {svc.name} 上没有可用的工具 {name}", tool=name
            )
        if not caller.allows(name):
            raise Rejection(
                Reason.FORBIDDEN,
                f"这把 API Key 没有被授权使用 {svc.name} · {name}，请联系服务所有者或 MCP 管理员",
                tool=name,
                key_uid=str(caller.key.subject_uid),
            )
        problem = check_arguments(tool.input_schema, arguments)
        if problem is not None:
            raise Rejection(Reason.INVALID_PARAMS, problem, tool=name)
        try:
            result = await self.upstreams.call(svc, tool, arguments, caller.context())
        except InvalidArguments as exc:
            raise Rejection(Reason.INVALID_PARAMS, f"参数转不成请求消息：{exc}", tool=name) from exc
        except UpstreamError as exc:
            raise Rejection(
                Reason.UPSTREAM_ERROR,
                f"MCP 服务 {svc.name} 的上游返回错误：{exc.message}",
                forwarded=True,
                tool=name,
                upstream_code=exc.code,
            ) from exc
        except UpstreamUnavailable as exc:
            logger.warning("MCP 服务 %s 的上游不可用：%s", svc.name, exc)
            raise Rejection(
                Reason.UPSTREAM_UNAVAILABLE,
                f"MCP 服务 {svc.name} 暂时不可用，请稍后重试",
                tool=name,
            ) from exc
        tracing.mark(outcome="tool_error" if result.get("isError") else "ok")
        return result

    # ------------------------------------------------------------------ SDK 的处理函数

    @staticmethod
    def _caller(ctx: Any) -> Caller:
        return ctx.request.state.mcpgw  # type: ignore[no-any-return]

    async def _list_tools(self, ctx: Any, params: Any) -> mt.ListToolsResult:
        caller = self._caller(ctx)
        return mt.ListToolsResult(
            tools=[
                mt.Tool.model_validate(
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.input_schema,
                        **({"annotations": t.annotations} if t.annotations else {}),
                    }
                )
                for t in await self.visible(caller)
            ]
        )

    async def _call_tool(self, ctx: Any, params: mt.CallToolRequestParams) -> mt.CallToolResult:
        caller = self._caller(ctx)
        try:
            result = await self.execute(caller, params.name, params.arguments or {})
        except Rejection as r:
            tracing.mark(outcome=r.reason.value)
            error = r.error(caller.service.name)
            raise MCPError(error["code"], error["message"], error["data"]) from r
        return mt.CallToolResult.model_validate(result)

    # ------------------------------------------------------------------ ASGI 入口

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.method != "POST":
            # 无状态：没有推送流（GET）、没有会话可关（DELETE）
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
        body = await request.body()
        message = _first(body)
        service = str(scope["path_params"]["service"])
        token = tracing.attach_incoming(request.headers, _meta(message))
        try:
            # ★ 外层一个 HTTP 级的 Span：认 Key、判权时被拒的请求也有 Span；
            #   SDK 的消息 Span 挂在它下面
            with _tracer.start_as_current_span(
                f"POST /mcp/{service}",
                kind=SpanKind.SERVER,
                attributes={
                    "http.request.method": "POST",
                    "url.path": str(scope.get("path") or ""),
                },
            ) as span:
                try:
                    caller = await self.admit(
                        service,
                        _bearer(request.headers),
                        user_uid=(request.headers.get("x-uid") or "").strip() or None,
                        audit={
                            name: value
                            for name in _AUDIT_HEADERS
                            if (value := (request.headers.get(name) or "").strip())
                        },
                    )
                except Rejection as r:
                    tracing.mark(service=service, outcome=r.reason.value)
                    span.set_attribute("error.type", r.reason.value)
                    span.set_status(StatusCode.ERROR, r.message)
                    out = {"jsonrpc": "2.0", "id": _rpc_id(message), "error": r.error(service)}
                    await JSONResponse(out, status_code=r.http_status)(scope, receive, send)
                    return
                scope.setdefault("state", {})["mcpgw"] = caller
                replayed = False

                async def replay() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                await self.manager.handle_request(scope, replay, send)
        finally:
            tracing.detach(token)
