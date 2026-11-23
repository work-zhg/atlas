"""MCP 端点：网关自己就是 MCP 服务端（MCP SDK 的 lowlevel Server，无状态、JSON 响应）。

    /mcp/{服务名}   POST（JSON-RPC）。GET、DELETE 回 405：无状态，没有推送流、没有会话可关

准入（认 Key、服务、判权）在交给 SDK 之前做：不过就回 HTTP 401 / 403 / 404 / 503 + JSON-RPC
错误体，被拒的请求也有自己的 Span。tools/list、tools/call 由 SDK 回调，交给工具调用
（invocation/service.py 的 ToolInvocation）。

★ 无状态：每个请求独立，任一实例都能处理；不发会话标识。
★ 只对外暴露工具：resources、prompts 不转发。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from opentelemetry import trace as otel
from opentelemetry.trace import SpanKind, StatusCode
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from ..invocation import trace
from ..invocation.rejection import Rejection
from ..invocation.service import Caller, ToolInvocation

__all__ = ["McpEndpoint"]

_tracer = otel.get_tracer("atlas_mcpgateway")

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


class McpEndpoint:
    def __init__(self, invocation: ToolInvocation) -> None:
        self.invocation = invocation
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
                for t in await self.invocation.visible_tools(caller)
            ]
        )

    async def _call_tool(self, ctx: Any, params: mt.CallToolRequestParams) -> mt.CallToolResult:
        caller = self._caller(ctx)
        try:
            result = await self.invocation.execute(caller, params.name, params.arguments or {})
        except Rejection as r:
            trace.mark(outcome=r.reason.value)
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
        token = trace.attach_incoming(request.headers, _meta(message))
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
                    caller = await self.invocation.admit(
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
                    trace.mark(service=service, outcome=r.reason.value)
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
            trace.detach(token)
