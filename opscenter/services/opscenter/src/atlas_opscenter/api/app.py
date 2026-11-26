from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from atlas_jobs import JobScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..access import CatalogSync, ModuleCatalog, PrincipalResolver, UserCenterOpen
from ..db.session import get_sessionmaker
from ..errors import OpsError
from ..gateway import OpsClient, SystemRegistry
from ..settings import OpsSettings, get_settings
from ..tenants import OperationSweeper, StepRunner
from . import audit, me, systems, tenants

__all__ = ["create_app"]


def create_app(
    *,
    settings: OpsSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    background: bool = True,
) -> FastAPI:
    """transport 可注入（测试里把各系统换成进程内的 ASGI 应用）；background=False 不起后台任务。"""
    settings = settings or get_settings()
    registry = SystemRegistry(settings.systems)
    client = OpsClient(registry, settings, transport=transport)
    modules = ModuleCatalog(registry)
    uc_open = UserCenterOpen(
        registry.user_center.base_url, timeout=settings.call_timeout_s, transport=transport
    )
    jobs = JobScheduler()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if background:
            sessions = get_sessionmaker()
            jobs.register(
                OperationSweeper(sessions, StepRunner(client), settings.lease_s),
                scope="instance",
                every=settings.sweep_interval_s,
            )
            # 自己的应用目录同步进用户中心、给系统租户开通：启动后马上一次，之后定时补
            jobs.register(
                CatalogSync(client, modules, settings.home_url),
                scope="instance",
                every=settings.catalog_sync_s,
            )
            jobs.start()
        try:
            yield
        finally:
            if background:
                await jobs.stop()
            await client.aclose()
            await uc_open.aclose()

    app = FastAPI(title="Atlas Ops Center", version="0.1.0", lifespan=lifespan)
    app.state.ops_client = client
    app.state.modules = modules
    app.state.resolver = PrincipalResolver(uc_open, modules, settings.authz_cache_s)
    app.state.catalog_sync = CatalogSync(client, modules, settings.home_url)
    app.state.jobs = jobs
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(OpsError)
    async def _error(_: Request, exc: OpsError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_FAILED",
                "message": "请求参数不正确",
                "details": {"errors": exc.errors()},
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for r in (me.router, audit.router, tenants.router, systems.router):
        app.include_router(r)
    return app
