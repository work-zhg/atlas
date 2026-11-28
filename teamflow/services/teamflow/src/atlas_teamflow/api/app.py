"""组装 TeamFlow 服务：客户端、访问控制、协调层、投递器、通知器、路由。

入口（c4-model/teamflow/component-server.html）：
    管理接口      /api/v1：团队、模板、市场、项目（mgmt.py）
    流程接口      /api/v1：流程、空间、实时事件、等你与通知（flow.py）
    节点对话接口  /api/v1：发消息、接管、交还（node_chat.py）
    运营接口      /ops/v1：租户的创建、删除（ops.py，atlas_opsapi）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from atlas_jobs import JobRoutes, JobScheduler
from atlas_tenancy import TenantScope, TenantViolation
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..access import AccessControl, PeopleDirectory, Principal
from ..agentplatform import AgentPlatform
from ..collab.dispatcher import InstructionDispatcher
from ..coord import LocalCoordinator, make_coordinator, set_coordinator
from ..db.session import get_sessionmaker
from ..errors import Forbidden, TFError
from ..inbox.channels import NotifyChannel
from ..inbox.notifier import Notifier
from ..jobs import DispatchSweep, Notify
from ..outbound import IdentityScopeMiddleware
from ..settings import TFSettings, get_settings
from ..usercenter import UserCenter
from . import flow, mgmt, node_chat
from .deps import build_services, current_principal
from .ops import mount_ops

__all__ = ["create_app"]

log = logging.getLogger("atlas_teamflow")


def create_app(
    settings: TFSettings | None = None,
    *,
    uc: UserCenter | None = None,
    atlas: AgentPlatform | None = None,
    coord: LocalCoordinator | None = None,
    channels: dict[str, NotifyChannel] | None = None,
) -> FastAPI:
    """uc / atlas / coord / channels 可注入（测试用内存实现）。"""
    settings = settings or get_settings()
    uc_client = uc or UserCenter(settings.uc_base_url)
    atlas_client = atlas or AgentPlatform(settings.atlas_base_url)
    coord = coord or make_coordinator(settings.redis_url)
    set_coordinator(coord)
    access = AccessControl(uc_client, ttl_seconds=settings.authz_ttl_seconds)
    coord.on_team_changed(access.drop_team)
    dispatcher = InstructionDispatcher(
        get_sessionmaker,
        atlas_client,
        lambda s: build_services(s, uc_client, atlas_client).process,
    )
    if settings.embedded_worker:
        coord.on_wake(dispatcher.on_wake)
    notifier = Notifier(get_sessionmaker, channels)
    jobs = JobScheduler()
    if settings.embedded_worker:
        jobs.register(
            DispatchSweep(dispatcher), scope="instance", every=settings.dispatch_sweep_seconds
        )
    jobs.register(Notify(notifier), scope="instance", every=settings.notify_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with TenantScope.isolated():
            await coord.start()
            jobs.start()
        yield
        await jobs.stop()
        await dispatcher.drain()
        await coord.close()
        if uc is None:
            await uc_client.aclose()
        if atlas is None:
            await atlas_client.aclose()

    app = FastAPI(title="AI TeamFlow", version="0.2.0", lifespan=lifespan)
    # ★ 在这里就挂上（不等 lifespan）：测试用的 ASGITransport 不跑 lifespan
    app.state.settings = settings
    app.state.uc = uc_client
    app.state.atlas = atlas_client
    app.state.access = access
    app.state.people = PeopleDirectory(uc_client)
    app.state.dispatcher = dispatcher
    app.state.notifier = notifier
    app.state.jobs = jobs
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(IdentityScopeMiddleware)

    @app.exception_handler(TFError)
    async def _tf_error(_: Request, exc: TFError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(TenantViolation)
    async def _tenant(_: Request, exc: TenantViolation) -> JSONResponse:
        log.error("租户上下文错误：%s", exc)
        return JSONResponse(status_code=500, content={"code": "INTERNAL", "message": "内部错误"})

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

    for r in (mgmt.router, flow.router, node_chat.router):
        app.include_router(r)
    _mount_jobs(app, jobs)
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    mount_ops(app, key, jobs)
    return app


class _OpsStaff:
    """定时任务的只读管理接口：系统租户的人（运营人员）才能看。鉴权在路由依赖里做完。"""

    async def view(self, request: Request) -> str:
        return str(request.state.job_viewer)


async def _job_viewer(request: Request, p: Principal = Depends(current_principal)) -> None:
    if not p.system_tenant:
        raise Forbidden("PERMISSION_DENIED", "只有运营人员能看定时任务")
    request.state.job_viewer = p.uid


def _mount_jobs(app: FastAPI, jobs: JobScheduler) -> None:
    routes: Any = JobRoutes(jobs, _OpsStaff(), prefix="/api/v1/jobs")
    app.include_router(routes.router, dependencies=[Depends(_job_viewer)])
