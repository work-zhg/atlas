"""设备网关的接口。

公网（前端网关 nginx 把 /device-api/ 转过来，不经登录校验，网关自己认令牌）：
    POST /pair                  连接器配对：配对码换设备令牌
    WS   /connect               连接器的控制连接
    *    /u/{上游编号}/…        能力代理：本机 CLI 拿会话令牌调模型、MCP、知识库

内部（只走集群内地址，nginx 挡掉 /internal）：
    GET    /internal/presence/{设备}           查在线
    POST   /internal/devices/{设备}/disconnect 吊销：断开设备、作废它上面所有会话的登记
    PUT    /internal/sessions/{会话}           登记本轮能力
    DELETE /internal/sessions/{会话}           作废会话登记
    WS     /internal/host/{会话}               接上本机会话（Agent 服务的上游连接连这里）
    POST   /internal/files/{设备}              读本机文件（列改动、列目录、列允许目录、取文件）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .capability import CapabilityProxy, SessionRegistry
from .link import AuthUnavailable, DeviceAuth, LinkServer
from .relay import FileRelay, HostRelay
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


class FileIn(BaseModel):
    op: str = Field(pattern="^(changes|list|roots|fetch)$")
    project_dir: str = ""
    path: str = ""
    byte_range: str | None = None
    if_none_match: str | None = None


def create_app(
    settings: GatewaySettings | None = None,
    *,
    redis: aioredis.Redis | None = None,
    agent_transport: httpx.AsyncBaseTransport | None = None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = redis or aioredis.from_url(settings.redis_url)
        agent_client = httpx.AsyncClient(timeout=10.0, transport=agent_transport, trust_env=False)
        proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=settings.proxy_connect_timeout_s),
            transport=proxy_transport,
            trust_env=False,
        )
        bindings = BindingStore(r, settings.binding_ttl_s)
        registry = SessionRegistry(bindings, settings.public_url)
        links = LinkServer(
            PresenceTable(r, settings.presence_ttl_s),
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
        app.state.proxy = CapabilityProxy(bindings, proxy_client)
        try:
            yield
        finally:
            await agent_client.aclose()
            await proxy_client.aclose()
            if redis is None:
                await r.aclose()

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

    @app.get("/internal/presence/{device}")
    async def presence(device: str, request: Request) -> Response:
        found = await request.app.state.links.presence(device)
        if found is None:
            return _error(404, "offline", "设备不在线")
        return JSONResponse(found)

    @app.post("/internal/devices/{device}/disconnect")
    async def disconnect(device: str, body: DisconnectIn, request: Request) -> dict[str, Any]:
        sessions = await request.app.state.registry.revoke_device(device)
        dropped = await request.app.state.links.disconnect(device, body.reason)
        return {"disconnected": dropped, "sessions": sessions}

    @app.put("/internal/sessions/{thread}")
    async def register(thread: str, body: RegisterIn, request: Request) -> dict[str, Any]:
        return await request.app.state.registry.register(  # type: ignore[no-any-return]
            thread,
            device=body.device,
            owner=body.owner,
            launch=body.launch,
            upstreams=[u.model_dump() for u in body.upstreams],
        )

    @app.delete("/internal/sessions/{thread}", status_code=204)
    async def revoke(thread: str, request: Request) -> Response:
        await request.app.state.registry.revoke(thread)
        return Response(status_code=204)

    @app.websocket("/internal/host/{thread}")
    async def host(ws: WebSocket, thread: str) -> None:
        await ws.app.state.host.serve(ws, thread)

    @app.post("/internal/files/{device}")
    async def files(device: str, body: FileIn, request: Request) -> Response:
        return await request.app.state.files.request(device, body.model_dump())  # type: ignore[no-any-return]

    return app


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"kind": kind, "message": message}}, status_code=status)
