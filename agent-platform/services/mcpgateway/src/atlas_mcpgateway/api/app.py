from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from atlas_jobs import JobScheduler
from atlas_opsapi import OpsGuard, OpsRoutes, TenantLifecycle
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ..access.identity import IdentityHeadersMiddleware
from ..access.key_access import AccessCache
from ..access.usercenter import UserCenter, UserCenterGateway
from ..db.models import Base, McpApiKey, McpAudit, McpDataObject, McpService, McpTool
from ..db.session import get_sessionmaker
from ..errors import GatewayError
from ..invocation import trace
from ..invocation.service import ToolInvocation
from ..maintainer import ToolRefresh
from ..settings import get_settings
from ..upstream.gateway import UpstreamGateway
from . import admin, internal
from .endpoint import McpEndpoint

__all__ = ["create_app"]

#: 数据面：/mcp/{服务名}。★ 带不带结尾的斜杠都接：MCP 客户端不跟随重定向
_MCP_PATHS = ("/mcp/{service}", "/mcp/{service}/")


class _McpRoute:
    """数据面的 ASGI 入口：转给 app 生命周期里建好的 MCP 端点。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await scope["app"].state.mcp_endpoint(scope, receive, send)


def create_app(*, uc: UserCenter | None = None) -> FastAPI:
    """uc 可注入 —— 测试用内存实现；不注入则按设置连用户中心。"""
    settings = get_settings()
    trace.setup()
    # 定时任务（刷新工具定义）：lifespan 里注册、启动；运营接口 /ops/v1/jobs 看运行情况
    jobs = JobScheduler()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UserCenterGateway | None = None
        if uc is None:
            owned = UserCenterGateway(settings.uc_base_url, snapshot_ttl_s=settings.authz_ttl_s)
            _wire(app, owned)
        upstream = UpstreamGateway(settings)
        app.state.upstream = upstream
        grant_secret = (
            settings.identity_grant_secret.get_secret_value()
            if settings.identity_grant_secret
            else ""
        )
        invocation = ToolInvocation(
            get_sessionmaker(), app.state.uc, app.state.access, upstream, grant_secret=grant_secret
        )
        app.state.invocation = invocation
        endpoint = McpEndpoint(invocation)
        app.state.mcp_endpoint = endpoint
        if settings.maintainer_tick_s > 0:
            jobs.register(
                ToolRefresh(settings, app.state.uc, upstream),
                scope="instance",
                every=settings.maintainer_tick_s,
            )
        await endpoint.start()
        jobs.start()
        try:
            yield
        finally:
            await endpoint.stop()
            await jobs.stop()
            await upstream.aclose()
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="MCP Gateway", version="0.2.0", lifespan=lifespan)
    app.state.jobs = jobs
    if uc is not None:
        _wire(app, uc)
    # 身份请求头透传：管理接口里调用户中心都带上（access/identity.py）
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
    _mount_ops(app, jobs)
    return app


class McpGatewayTenantLifecycle(TenantLifecycle):
    """MCP 网关的租户接入。新租户不用准备什么（服务、Key 都由租户自己建，平台服务所有租户可见）；
    删除只有数据库里的行，用户中心里登记的服务、Key 的主体随租户在用户中心里一起删。"""

    purge_order = (McpAudit, McpDataObject, McpTool, McpApiKey, McpService)

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        return None


def _mount_ops(app: FastAPI, jobs: JobScheduler) -> None:
    """运营接口（/ops/v1）：租户的创建、删除，系统任务（刷新工具定义）。"""
    settings = get_settings()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    app.state.ops_guard = guard = OpsGuard(key)
    OpsRoutes.mount(app, guard, McpGatewayTenantLifecycle(get_sessionmaker(), base=Base), jobs=jobs)


def _wire(app: FastAPI, uc: UserCenter) -> None:
    app.state.uc = uc
    app.state.access = AccessCache(uc, get_settings().perm_cache_ttl_s)
