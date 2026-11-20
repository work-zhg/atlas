from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from atlas_jobs import JobScheduler
from atlas_tenancy import TenantScope
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..auth import IdentityHeadersMiddleware
from ..errors import KnowledgeError
from ..jobs import IngestSweep
from ..runtime import Runtime, build_runtime
from ..settings import get_settings
from ..storage import StorageNotConfigured
from ..telemetry import setup_telemetry, shutdown_telemetry, trace_requests
from . import datasets, internal, sets
from .mcp import build_mcp, mcp_app
from .ops import mount_ops

__all__ = ["create_app"]

logger = logging.getLogger(__name__)


def create_app(*, runtime: Runtime | None = None, cors_origins: list[str] | None = None) -> FastAPI:
    """runtime 可注入 —— 测试用内存 Qdrant、假模型、内联投递。"""

    mcp = build_mcp(lambda: app.state.runtime)
    # 定时任务（入库巡检）：lifespan 里注册、启动；运营接口 /ops/v1/jobs 看运行情况
    jobs = JobScheduler()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 后台任务（入库巡检）不继承启动方的租户：它在系统范围里取待办，再逐个进入租户
        with TenantScope.isolated():
            async with _lifespan(app):
                yield

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        app.state.runtime = runtime or build_runtime(get_settings())
        # 和运营接口用同一个：停用后本进程的入库巡检立刻跳过它
        app.state.runtime.tenant_gate = app.state.tenant_gate
        # 存储份额：上传前判，和运营接口改份额用同一个；系统参数在这里登记（没有的补默认值）
        app.state.runtime.capacity = app.state.capacity
        await app.state.capacity.config.register()
        if owned:
            setup_telemetry(app.state.runtime.settings)
        interval = app.state.runtime.settings.ingest_sweep_interval_s
        if interval > 0:
            jobs.register(IngestSweep(app.state.runtime), scope="instance", every=interval)
        jobs.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await jobs.stop()
            if owned:
                await app.state.runtime.aclose()
                shutdown_telemetry()

    app = FastAPI(title="Atlas Knowledge", version="0.1.0", lifespan=lifespan)
    app.state.jobs = jobs
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 取请求头里的 traceparent：检索 span 挂在 atlas-server 那次调用之下
    trace_requests(app)
    # 身份请求头透传：本请求里调用户中心 / atlas-server 都带上（auth.py）
    app.add_middleware(IdentityHeadersMiddleware)

    @app.exception_handler(KnowledgeError)
    async def _error(_: Request, exc: KnowledgeError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, **({"context": exc.detail} if exc.detail else {})},
        )

    @app.exception_handler(StorageNotConfigured)
    async def _no_storage(_: Request, exc: StorageNotConfigured) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(internal.router)
    app.include_router(datasets.router)
    app.include_router(sets.router)
    # 运营接口：租户的停用、恢复、删除，系统任务
    mount_ops(app, jobs)
    # Pod 里的 ACP CLI 经它调 knowledge_search（令牌鉴权，见 api/mcp.py）
    app.mount("/mcp", mcp_app(mcp))
    return app
