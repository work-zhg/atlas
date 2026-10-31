from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from .. import tracing
from ..auth import IdentityHeadersMiddleware
from ..dataplane import DataPlane
from ..db.session import get_sessionmaker
from ..errors import GatewayError
from ..maintainer import run_maintainer
from ..perms import AccessCache, Permissions, UCPermissions
from ..settings import get_settings
from ..upstreams.hub import Upstreams
from . import admin, internal

__all__ = ["create_app"]

#: 数据面：/mcp/{服务名}。★ 带不带结尾的斜杠都接：MCP 客户端不跟随重定向
_MCP_PATHS = ("/mcp/{service}", "/mcp/{service}/")


class _McpRoute:
    """数据面的 ASGI 入口：转给 app 生命周期里建好的 DataPlane。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await scope["app"].state.data_plane(scope, receive, send)


def create_app(*, perms: Permissions | None = None) -> FastAPI:
    """perms 可注入 —— 测试用内存实现；不注入则按设置连用户中心。"""
    settings = get_settings()
    tracing.setup()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UCPermissions | None = None
        if perms is None:
            owned = UCPermissions(settings.uc_base_url)
            _wire(app, owned)
        upstreams = Upstreams(settings)
        app.state.upstreams = upstreams
        plane = DataPlane(get_sessionmaker(), app.state.perms, app.state.access, upstreams)
        app.state.data_plane = plane
        maintainer = (
            asyncio.create_task(run_maintainer(settings, app.state.perms, upstreams))
            if settings.maintainer_tick_s > 0
            else None
        )
        await plane.start()
        try:
            yield
        finally:
            await plane.stop()
            if maintainer is not None:
                maintainer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintainer
            await upstreams.aclose()
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="MCP Gateway", version="0.2.0", lifespan=lifespan)
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

    route: Any = _McpRoute()
    for path in _MCP_PATHS:
        app.add_route(path, route, methods=["GET", "POST", "DELETE"], include_in_schema=False)

    app.include_router(admin.router)
    app.include_router(internal.router)
    return app


def _wire(app: FastAPI, perms: Permissions) -> None:
    app.state.perms = perms
    app.state.access = AccessCache(perms, get_settings().perm_cache_ttl_s)
