"""设备网关的接口。

公网（API 网关把 /device/ 转过来，不验 JWT，这里自己认令牌）：
    POST /pair                  连接器配对：配对码换设备令牌
    WS   /connect               连接器的控制连接
    *    /u/{上游编号}/…        能力代理：本机 CLI 拿会话令牌调模型、MCP、知识库

内部（只走集群内地址，API 网关挡掉 /internal）：
    GET    /internal/presence/{设备}           查在线
    POST   /internal/devices/{设备}/disconnect 吊销：断开设备、作废它上面所有会话的登记
    PUT    /internal/sessions/{会话}           登记本轮能力
    DELETE /internal/sessions/{会话}           作废会话登记
    WS     /internal/host/{会话}               接上本机会话（Agent 服务的上游连接连这里）
    POST   /internal/files/{设备}              读本机文件（列改动、列目录、列允许目录、取文件）
    POST   /internal/browser/{设备}            本机浏览器：开、关接管窗口，剪贴板，状态
    WS     /internal/view/{设备}?thread=&screen=  看本机浏览器的画面（画面流）
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from atlas_redis import RedisConfig, RedisRuntime
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .capability import CapabilityProxy, SessionRegistry
from .link import AuthUnavailable, DeviceAuth, LinkServer
from .relay import BrowserRelay, FileRelay, HostRelay, ViewRelay
from .settings import GatewaySettings, get_settings
from .store import BindingStore, PresenceTable

__all__ = ["create_app"]

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    report: dict[str, Any] = Field(default_factory=dict)


class UpstreamIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    kind: str = ""
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class RegisterIn(BaseModel):
    device: str
    owner: str
    launch: dict[str, Any]
    upstreams: list[UpstreamIn] = Field(default_factory=list)


class DisconnectIn(BaseModel):
    reason: str = "设备已吊销"


_BROWSER_OPS = r"^(takeover\.open|takeover\.close|clipboard\.set|clipboard\.get|status)$"


class BrowserIn(BaseModel):
    op: str = Field(pattern=_BROWSER_OPS)
    #: 会话（根会话 id 在 screen 里）
    thread: str = ""
    screen: str = ""
    takeover: str = ""
    url: str = ""
    text: str | None = None


class FileIn(BaseModel):
    op: str = Field(pattern="^(changes|list|roots|fetch)$")
    project_dir: str = ""
    path: str = ""
    byte_range: str | None = None
    if_none_match: str | None = None


def create_app(
    settings: GatewaySettings | None = None,
    *,
    redis: Any | None = None,
    agent_transport: httpx.AsyncBaseTransport | None = None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        redis_rt: RedisRuntime | None = None
        if redis is None:
            redis_rt = RedisRuntime(RedisConfig(url=settings.redis_url, service="devicegw"))
            r: Any = redis_rt.commands
            scripts = redis_rt.scripts
        else:
            r = redis
            scripts = None
        agent_client = httpx.AsyncClient(timeout=10.0, transport=agent_transport, trust_env=False)
        proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=settings.proxy_connect_timeout_s),
            transport=proxy_transport,
            trust_env=False,
        )
        bindings = BindingStore(r, settings.binding_ttl_s)
        registry = SessionRegistry(bindings, settings.public_url)
        links = LinkServer(
            PresenceTable(r, settings.presence_ttl_s, scripts=scripts),
            DeviceAuth(settings.agent_url, agent_client),
            instance_url=settings.own_url(),
            min_version=settings.min_connector_version,
            presence_ttl_s=settings.presence_ttl_s,
            stall_s=settings.stall_s,
        )
        app.state.links = links
        app.state.registry = registry
        app.state.host = HostRelay(links, registry, internal_token=settings.internal_token)
        app.state.files = FileRelay(links)
        app.state.browser = BrowserRelay(links)
        app.state.view = ViewRelay(links, internal_token=settings.internal_token)
        app.state.proxy = CapabilityProxy(bindings, proxy_client)
        try:
            yield
        finally:
            await agent_client.aclose()
            await proxy_client.aclose()
            if redis_rt is not None:
                await redis_rt.aclose()

    app = FastAPI(title="Atlas device gateway", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------ 公网

    @app.post("/pair")
    async def pair(body: PairIn, request: Request) -> Response:
        try:
            issued = await request.app.state.links.pair(body.code.strip().upper(), body.report)
        except AuthUnavailable:
            return _error(503, "unavailable", "平台暂时不可用，稍后再试")
        if issued is None:
            return _error(400, "invalid_code", "配对码无效或已过期，请在网页上重新获取")
        return JSONResponse(issued)

    @app.websocket("/connect")
    async def connect(ws: WebSocket) -> None:
        await ws.app.state.links.accept(ws)

    @app.api_route("/u/{upstream_id}", methods=_METHODS, include_in_schema=False)
    async def proxy_root(upstream_id: str, request: Request) -> Response:
        return await request.app.state.proxy.handle(request, upstream_id, "")

    @app.api_route("/u/{upstream_id}/{path:path}", methods=_METHODS, include_in_schema=False)
    async def proxy(upstream_id: str, path: str, request: Request) -> Response:
        return await request.app.state.proxy.handle(request, upstream_id, path)

    # ------------------------------------------------------------------ 内部

    def internal_only(request: Request) -> None:
        """内部 HTTP 接口校验服务令牌，和 WS 的两条（host、view）一样。

        ★ nginx 挡掉 /internal 只是第一道：集群里谁连得上设备网关，谁就能登记会话能力、
          读使用者电脑上的文件。没配令牌（本机开发）不校验。
        """
        token = settings.internal_token
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "缺少或错误的服务令牌")

    internal = [Depends(internal_only)]

    @app.get("/internal/presence/{device}", dependencies=internal)
    async def presence(device: str, request: Request) -> Response:
        found = await request.app.state.links.presence(device)
        if found is None:
            return _error(404, "offline", "设备不在线")
        return JSONResponse(found)

    @app.post("/internal/devices/{device}/disconnect", dependencies=internal)
    async def disconnect(device: str, body: DisconnectIn, request: Request) -> dict[str, Any]:
        sessions = await request.app.state.registry.revoke_device(device)
        dropped = await request.app.state.links.disconnect(device, body.reason)
        return {"disconnected": dropped, "sessions": sessions}

    @app.put("/internal/sessions/{thread}", dependencies=internal)
    async def register(thread: str, body: RegisterIn, request: Request) -> dict[str, Any]:
        return await request.app.state.registry.register(  # type: ignore[no-any-return]
            thread,
            device=body.device,
            owner=body.owner,
            launch=body.launch,
            upstreams=[u.model_dump() for u in body.upstreams],
        )

    @app.delete("/internal/sessions/{thread}", status_code=204, dependencies=internal)
    async def revoke(thread: str, request: Request) -> Response:
        await request.app.state.registry.revoke(thread)
        return Response(status_code=204)

    @app.websocket("/internal/host/{thread}")
    async def host(ws: WebSocket, thread: str) -> None:
        await ws.app.state.host.serve(ws, thread)

    @app.post("/internal/files/{device}", dependencies=internal)
    async def files(device: str, body: FileIn, request: Request) -> Response:
        return await request.app.state.files.request(device, body.model_dump())  # type: ignore[no-any-return]

    @app.post("/internal/browser/{device}", dependencies=internal)
    async def browser(device: str, body: BrowserIn, request: Request) -> Response:
        payload = body.model_dump(exclude_none=True)
        return await request.app.state.browser.request(device, payload)  # type: ignore[no-any-return]

    @app.websocket("/internal/view/{device}")
    async def view(ws: WebSocket, device: str, thread: str = "", screen: str = "") -> None:
        await ws.app.state.view.serve(ws, device, thread, screen)

    return app


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"kind": kind, "message": message}}, status_code=status)
