from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..agents.supervisor import AgentSupervisor
from ..atlas import AtlasClient
from ..db.session import get_sessionmaker
from ..errors import TFError
from ..identity import TeamLevelCache
from ..process.bus import LocalCoordinator, make_coordinator, set_coordinator
from ..process.git_store import make_git_store
from ..settings import TFSettings, get_settings
from ..uc import UCClient
from . import auth, directory, file_templates, flow_templates, processes, projects, teams

__all__ = ["check_deployment", "create_app"]


def check_deployment(settings: TFSettings) -> None:
    """配置组合的硬性约束：不满足就拒绝启动，而不是运行起来才出错。"""
    if not settings.embedded_worker and not settings.redis_url:
        raise RuntimeError(
            "关闭内嵌监督器（ATLAS_TF_EMBEDDED_WORKER=false）需要配置 ATLAS_TF_REDIS_URL："
            "否则独立 worker 收不到唤醒"
        )
    if settings.git_backend == "gitee" and not settings.gitee_token.get_secret_value():
        raise RuntimeError("ATLAS_TF_GIT_BACKEND=gitee 需要配置 ATLAS_TF_GITEE_TOKEN")


def create_app(
    settings: TFSettings | None = None,
    *,
    uc: UCClient | None = None,
    atlas: AtlasClient | None = None,
    coord: LocalCoordinator | None = None,
) -> FastAPI:
    """uc / atlas / coord 可注入（测试用内存实现）。"""
    settings = settings or get_settings()
    check_deployment(settings)

    uc_client = uc or UCClient(settings)
    atlas_client = atlas or AtlasClient(settings)
    coord = coord or make_coordinator(settings.redis_url, settings.worker_lease_seconds)
    set_coordinator(coord)
    git = make_git_store(settings)
    levels = TeamLevelCache(settings.team_level_ttl_seconds)
    coord.on_team_changed(levels.invalidate)  # 其他实例改了成员 → 本实例的级别缓存失效
    supervisor = (
        AgentSupervisor(
            get_sessionmaker,
            atlas_client,
            git,
            lease_seconds=settings.worker_lease_seconds,
            sweep_seconds=settings.worker_sweep_seconds,
        )
        if settings.embedded_worker
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await coord.start()
        await git.prepare()
        if supervisor:
            await supervisor.start()  # 续接未完成的 Agent 运行与排队的指令，并定期扫描
        yield
        if supervisor:
            await supervisor.close()
        await coord.close()
        if uc is None:
            await uc_client.aclose()
        if atlas is None:
            await atlas_client.aclose()

    app = FastAPI(title="AI TeamFlow", version="0.1.0", lifespan=lifespan)
    # ★ 客户端在这里就挂上（不等 lifespan）：测试用的 ASGITransport 不跑 lifespan
    app.state.uc = uc_client
    app.state.atlas = atlas_client
    app.state.levels = levels
    app.state.git = git
    app.state.supervisor = supervisor
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(TFError)
    async def _tf_error(_: Request, exc: TFError) -> JSONResponse:
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

    for r in (
        auth.router,
        directory.router,
        file_templates.router,
        flow_templates.router,
        teams.router,
        projects.router,
        processes.router,
    ):
        app.include_router(r)
    return app
