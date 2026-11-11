from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from atlas_jobs import JobScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..agents.supervisor import AgentSupervisor
from ..atlas import AtlasClient
from ..db.session import get_sessionmaker
from ..errors import TFError
from ..gitee_oauth import GiteeOAuth
from ..identity import TeamLevelCache
from ..jobs import CollaboratorReconcile, NodeSweep
from ..outbound import IdentityScopeMiddleware
from ..process.bus import LocalCoordinator, make_coordinator, set_coordinator
from ..process.repo_host import RepoHost, make_repo_host
from ..settings import TFSettings, get_settings
from ..uc import UCClient
from . import (
    account,
    atlas_proxy,
    auth,
    directory,
    file_templates,
    flow_templates,
    processes,
    projects,
    teams,
)
from .jobs import jobs_router

__all__ = ["check_deployment", "create_app"]

log = logging.getLogger("atlas_teamflow")


def check_deployment(settings: TFSettings) -> None:
    """配置组合的硬性约束：不满足就拒绝启动，而不是运行起来才出错。"""
    if not settings.embedded_worker and not settings.redis_url:
        raise RuntimeError(
            "关闭内嵌监督器（ATLAS_TF_EMBEDDED_WORKER=false）需要配置 ATLAS_TF_REDIS_URL："
            "否则独立 worker 收不到唤醒"
        )
    if not settings.gitee_token.get_secret_value():
        raise RuntimeError(
            "需要配置 ATLAS_TF_GITEE_TOKEN（产物仓库管理令牌：建仓库、读提交、管理协作者）"
        )


def create_app(
    settings: TFSettings | None = None,
    *,
    uc: UCClient | None = None,
    atlas: AtlasClient | None = None,
    coord: LocalCoordinator | None = None,
    repo: RepoHost | None = None,
    oauth: GiteeOAuth | None = None,
) -> FastAPI:
    """uc / atlas / coord / repo / oauth 可注入（测试用内存实现）。"""
    settings = settings or get_settings()
    if repo is None:
        check_deployment(settings)

    uc_client = uc or UCClient(settings)
    atlas_client = atlas or AtlasClient(settings)
    coord = coord or make_coordinator(settings.redis_url, settings.worker_lease_seconds)
    set_coordinator(coord)
    host = repo or make_repo_host(settings)
    gitee_oauth = oauth or GiteeOAuth(settings)
    levels = TeamLevelCache(settings.team_level_ttl_seconds)
    coord.on_team_changed(levels.invalidate)  # 其他实例改了成员 → 本实例的级别缓存失效
    supervisor = (
        AgentSupervisor(
            get_sessionmaker,
            atlas_client,
            host,
            lease_seconds=settings.worker_lease_seconds,
            git_profile=settings.atlas_git_profile,
        )
        if settings.embedded_worker
        else None
    )
    # 定时任务：协作者对账、扫描待处理节点（内嵌监督器时）；lifespan 里启动，/api/v1/jobs 看运行情况
    jobs = JobScheduler()
    if settings.collaborator_reconcile_seconds > 0:
        jobs.register(
            CollaboratorReconcile(uc_client, host),
            scope="instance",
            every=settings.collaborator_reconcile_seconds,
        )
    if supervisor:
        jobs.register(NodeSweep(supervisor), scope="instance", every=settings.worker_sweep_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await coord.start()
        await host.prepare()
        if supervisor:
            await supervisor.start()  # 续接未完成的 Agent 运行与排队的指令（定期扫描是 NodeSweep）
        jobs.start()
        yield
        await jobs.stop()
        if supervisor:
            await supervisor.close()
        await coord.close()
        if uc is None:
            await uc_client.aclose()
        if atlas is None:
            await atlas_client.aclose()
        if oauth is None:
            await gitee_oauth.aclose()

    app = FastAPI(title="AI TeamFlow", version="0.1.0", lifespan=lifespan)
    # ★ 客户端在这里就挂上（不等 lifespan）：测试用的 ASGITransport 不跑 lifespan
    app.state.settings = settings
    app.state.uc = uc_client
    app.state.atlas = atlas_client
    app.state.levels = levels
    app.state.repo = host
    app.state.gitee_oauth = gitee_oauth
    app.state.supervisor = supervisor
    app.state.jobs = jobs
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # 每个请求一个干净的出站身份（主体解析时设置，见 outbound.py）
    app.add_middleware(IdentityScopeMiddleware)

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
        atlas_proxy.router,
        account.router,
        jobs_router(jobs),
    ):
        app.include_router(r)
    return app
