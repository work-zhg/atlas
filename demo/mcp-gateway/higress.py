"""Higress（mock）：REST 转 MCP、保管后端凭据；只认 MCP 网关。

    /mcp-servers/weather/   天气（REST 转 MCP，只读）
    /mcp-servers/ticket/    工单（REST 转 MCP）：create_ticket、delete_ticket
    /mcp-servers/report/    日报 —— ★ 故意漏开认证，演示 MCP 网关登记时的接入检查

每个服务前面一层 RequireGatewayCredential：
    · 只接受 Authorization: Bearer <网关凭据>，持有它的只有 MCP 网关；其余一律 401，并记日志、告警
    · 校验后去掉网关凭据再往里走：工具实现（以及它调的后端）看不到它
真实的 Higress 用模板把 REST 声明成 MCP；这里用 MCPServer 手写同样的转换：
工具参数 → 拼 REST 请求 → 带上后端凭据和身份请求头 → 把返回转成工具结果。

★ 不认人、不判权、不复核：这些都在 MCP 网关。Higress 只回答"这个请求是不是 MCP 网关发来的"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
from hops import hop
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

#: 转给后端的身份请求头（MCP 网关写入的）
IDENTITY_HEADERS = ("x-auth-uid", "x-uid", "x-run-id")


def _identity(ctx: Context) -> dict[str, str]:  # type: ignore[type-arg]
    headers = ctx.headers or {}
    return {h: headers[h] for h in IDENTITY_HEADERS if h in headers}


def _weather(backend: str, key: str) -> MCPServer:
    mcp = MCPServer("weather")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_weather(city: str) -> str:
        """查询某个城市今天的天气。"""
        hop("Higress", "REST 转 MCP：get_weather → POST /weather（带后端凭据）")
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{backend}/weather", json={"city": city}, headers={"X-Api-Key": key}
            )
        return r.json()["text"]

    return mcp


def _ticket(backend: str, key: str) -> MCPServer:
    mcp = MCPServer("ticket")

    @mcp.tool()
    async def create_ticket(title: str, ctx: Context) -> str:  # type: ignore[type-arg]
        """在工单系统里建一张工单，返回工单号。"""
        ident = _identity(ctx)
        hop(
            "Higress",
            f"REST 转 MCP：create_ticket → POST /tickets（带后端凭据，转交身份请求头 {ident}）",
        )
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{backend}/tickets", json={"title": title}, headers={"X-Api-Key": key, **ident}
            )
        return r.json()["text"]

    @mcp.tool()
    async def delete_ticket(ticket_id: str) -> str:
        """删除一张工单。"""
        return f"已删除 {ticket_id}"

    return mcp


def _report() -> MCPServer:
    mcp = MCPServer("report")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def daily_report() -> str:
        """今天的运营日报。"""
        return "日报：一切正常"

    return mcp


class RequireGatewayCredential:
    """纯 ASGI 中间件：只放行带网关凭据的请求，放行前去掉它。"""

    def __init__(self, service: str, app: Any, credential: str | None) -> None:
        self.service, self.app, self.credential = service, app, credential

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self.credential is None:  # None = 这个服务漏开了认证
            await self.app(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope["headers"] if k != b"authorization"]
        got = dict(scope["headers"]).get(b"authorization", b"").decode()
        if got != f"Bearer {self.credential}":
            client = (scope.get("client") or ("?",))[0]
            carried = f"带的是 {got[:16]}…" if got else "什么凭据都没带"
            hop(
                "Higress",
                f"拒绝 {scope['method']} /mcp-servers/{self.service}/：没有网关凭据"
                f"（来源 {client}，{carried}）→ 记日志、告警",
            )
            await JSONResponse({"error": "gateway credential required"}, status_code=401)(
                scope, receive, send
            )
            return
        await self.app({**scope, "headers": headers}, receive, send)


def app(*, credential: str, backend: str, backend_keys: dict[str, str]) -> Starlette:
    servers = {
        "weather": (_weather(backend, backend_keys["weather"]), credential),
        "ticket": (_ticket(backend, backend_keys["ticket"]), credential),
        "report": (_report(), None),
    }
    routes = [
        Mount(
            f"/mcp-servers/{name}",
            app=RequireGatewayCredential(
                name,
                mcp.streamable_http_app(
                    streamable_http_path="/",
                    stateless_http=True,
                    json_response=True,
                    transport_security=TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                ),
                cred,
            ),
        )
        for name, (mcp, cred) in servers.items()
    ]

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # 挂在 Mount 下的子应用不会跑自己的 lifespan：MCPServer 的会话管理器要在这里启动
        async with AsyncExitStack() as stack:
            for mcp, _cred in servers.values():
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
