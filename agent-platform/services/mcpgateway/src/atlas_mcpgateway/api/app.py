from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..auth import IdentityHeadersMiddleware
from ..db.session import get_sessionmaker
from ..errors import GatewayError
from ..maintainer import run_maintainer
from ..perms import Permissions, UCPermissions, UsableCache
from ..proxy import DataPlane
from ..settings import get_settings
from . import admin, internal

__all__ = ["create_app"]

#: 数据面：/mcp/{服务名}。★ 带不带结尾的斜杠都接：MCP 客户端不跟随重定向
_MCP_PATHS = ("/mcp/{service}", "/mcp/{service}/")


def create_app(*, perms: Permissions | None = None) -> FastAPI:
    """perms 可注入 —— 测试用内存实现；不注入则按设置连用户中心。"""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UCPermissions | None = None
        if perms is None:
            owned = UCPermissions(settings.uc_base_url)
            _wire(app, owned)
        # ★ 不设读超时：工具调用可能要几十秒、推送流更长（调用超时由调用方判）
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s, read=None, write=60.0, pool=30.0
            ),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
            trust_env=False,
        )
        app.state.data_plane = DataPlane(settings, get_sessionmaker(), app.state.usable, http)
        maintainer = (
            asyncio.create_task(run_maintainer(settings))
            if settings.maintainer_tick_s > 0
            else None
        )
        try:
            yield
        finally:
            if maintainer is not None:
                maintainer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintainer
            await http.aclose()
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="MCP Gateway", version="0.1.0", lifespan=lifespan)
    if perms is not None:
        _wire(app, perms)
    # 身份请求头透传：管理接口里调用户中心都带上（auth.py）
    app.add_middleware(IdentityHeadersMiddleware)

    @app.exception_handler(GatewayError)
    async def _error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.message,
                "code": exc.code,
                **({"context": exc.context} if exc.context else {}),
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def mcp(request: Request) -> object:
        return await request.app.state.data_plane.handle(request)

    for path in _MCP_PATHS:
        app.add_route(path, mcp, methods=["GET", "POST", "DELETE"], include_in_schema=False)

    app.include_router(admin.router)
    app.include_router(internal.router)
    return app


def _wire(app: FastAPI, perms: Permissions) -> None:
    app.state.perms = perms
    app.state.usable = UsableCache(perms, get_settings().perm_cache_ttl_s)
