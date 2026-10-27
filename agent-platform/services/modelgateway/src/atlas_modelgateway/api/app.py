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
from ..perms import Permissions, SubjectsCache, UCPermissions
from ..proxy import ENDPOINTS, DataPlane
from ..settings import get_settings
from ..usage import Recorder
from . import admin, internal

__all__ = ["create_app"]


def create_app(*, perms: Permissions | None = None) -> FastAPI:
    """perms 可注入 —— 测试用内存实现；不注入则按设置连用户中心。"""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UCPermissions | None = None
        if perms is None:
            owned = UCPermissions(settings.uc_base_url)
            _wire(app, owned)
        # ★ 不设读超时：思考型模型的一次调用可能要好几分钟（卡死由调用方按静默判断）
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s, read=None, write=120.0, pool=30.0
            ),
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200),
            trust_env=False,
        )
        app.state.data_plane = DataPlane(
            settings, get_sessionmaker(), app.state.subjects, http, app.state.recorder
        )
        maintainer = (
            asyncio.create_task(run_maintainer(settings, app.state.recorder))
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
            await app.state.recorder.flush()
            await http.aclose()
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="Model Gateway", version="0.1.0", lifespan=lifespan)
    app.state.recorder = Recorder(get_sessionmaker())
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

    for endpoint in ENDPOINTS.values():

        async def call(request: Request, _endpoint: object = endpoint) -> object:
            return await request.app.state.data_plane.handle(request, _endpoint)

        app.add_route(endpoint.path, call, methods=["POST"], include_in_schema=False)

    async def models(request: Request) -> object:
        return await request.app.state.data_plane.models(request)

    app.add_route("/v1/models", models, methods=["GET"], include_in_schema=False)

    app.include_router(admin.router)
    app.include_router(internal.router)
    return app


def _wire(app: FastAPI, perms: Permissions) -> None:
    settings = get_settings()
    app.state.perms = perms
    app.state.subjects = SubjectsCache(perms, settings.perm_cache_ttl_s, settings.perm_stale_max_s)
