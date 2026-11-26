"""管理平台（serve 子命令）：管理接口、内部接口、运营接口、定时任务。

模型调用不经过这里（那是 gateway 子命令）。网关状态（Redis）在这里只用来：读线路冷却、账号异常
（页面上的状态），解除账号异常，按租户递增配置版本号并广播即时失效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from atlas_jobs import JobScheduler
from atlas_opsapi import OpsGuard, OpsRoutes, TenantLifecycle
from atlas_redis import RedisConfig, RedisRuntime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import notify
from ..auth import IdentityHeadersMiddleware
from ..crypto import SecretCipher
from ..db.models import Base, GwAccount, GwApiKey, GwAudit, GwModel, GwModelCall, GwRoute
from ..db.session import get_sessionmaker
from ..errors import GatewayError
from ..jobs import AccountCheck, CallRetention, KeyRefresh
from ..perms import Permissions, SubjectsCache, UCPermissions
from ..provider import ProviderCatalog
from ..settings import get_settings
from ..shared.health import RouteHealth
from ..shared.upstream import AddressGuard, UpstreamClient
from . import admin, internal

__all__ = ["create_app"]


def create_app(
    *,
    perms: Permissions | None = None,
    redis: Any | None = None,
    probe_transport: httpx.AsyncBaseTransport | None = None,
    redis_factory: Callable[[str], Any] | None = None,
) -> FastAPI:
    """perms、redis、probe_transport 可注入 —— 测试用内存实现、本机 Redis、假供应商。"""
    settings = get_settings()
    # 定时任务（检查账号、重算有效模型、删旧的调用记录）：运营接口 /ops/v1/jobs 看运行情况
    jobs = JobScheduler()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_perms: UCPermissions | None = None
        if perms is None:
            owned_perms = UCPermissions(settings.uc_base_url)
            _wire_perms(app, owned_perms)
        owned_redis = None
        redis_rt: RedisRuntime | None = None
        state = redis
        if state is None and settings.redis_url:
            if redis_factory is not None:
                state = owned_redis = redis_factory(settings.redis_url)
            else:
                redis_rt = RedisRuntime(
                    RedisConfig(url=settings.redis_url, service="modelgw-admin")
                )
                state = redis_rt.commands
        app.state.redis_rt = redis_rt
        app.state.redis_state = state
        notify.use_redis(state, pubsub=redis_rt.pubsub if redis_rt is not None else None)
        app.state.health = health = RouteHealth(state)
        guard = app.state.guard
        app.state.probe_client = client = UpstreamClient(
            guard=guard, proxy=settings.egress_proxy, transport=probe_transport
        )
        sm = get_sessionmaker()
        if settings.account_check_interval_s > 0:
            jobs.register(
                AccountCheck(
                    sm,
                    cipher=app.state.cipher,
                    catalog=app.state.catalog,
                    guard=guard,
                    health=health,
                    client=client,
                    redis=state,
                ),
                scope="instance",
                every=settings.account_check_interval_s,
            )
        if settings.key_refresh_interval_s > 0:
            jobs.register(
                KeyRefresh(sm, app.state.perms, app.state.subjects, app.state.cipher, redis=state),
                scope="instance",
                every=settings.key_refresh_interval_s,
            )
            jobs.register(
                CallRetention(sm, settings.call_retention_months, redis=state),
                scope="instance",
                every=86_400,
            )
        jobs.start()
        try:
            yield
        finally:
            await jobs.stop()
            await client.aclose()
            notify.use_redis(None)
            if owned_perms is not None:
                await owned_perms.aclose()
            if redis_rt is not None:
                await redis_rt.aclose()
            elif owned_redis is not None:
                await owned_redis.aclose()

    app = FastAPI(title="Model Platform", version="1.0.0", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.catalog = ProviderCatalog()
    app.state.cipher = SecretCipher(settings.master_keys())
    app.state.guard = AddressGuard(settings.upstream_allow_cidrs)
    if perms is not None:
        _wire_perms(app, perms)
    # 身份请求头透传 + 每个请求从「没有租户」开始（auth.py）
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

    app.include_router(admin.router)
    app.include_router(internal.router)
    _mount_ops(app, jobs)
    return app


class ModelGatewayTenantLifecycle(TenantLifecycle):
    """模型平台的租户接入。新租户不用准备什么（模型、Key 都由租户自己建）；删除只有数据库里的
    行，用户中心里登记的模型数据对象随租户在用户中心里一起删。调用记录是分区表，按租户删也一样。

    ★ 不知道租户状态：租户停用、到期后，管理接口里用户中心对这个人回答无权限；用户 Key 的
      私有模型由「重算用户 Key 的有效模型」任务收回（用户中心同样回答无权限）。
    """

    purge_order = (GwModelCall, GwRoute, GwAccount, GwApiKey, GwModel, GwAudit)

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        return None


def _mount_ops(app: FastAPI, jobs: JobScheduler) -> None:
    """运营接口（/ops/v1）：租户的创建、删除，系统任务。"""
    settings = get_settings()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    app.state.ops_guard = guard = OpsGuard(key)
    OpsRoutes.mount(
        app, guard, ModelGatewayTenantLifecycle(get_sessionmaker(), base=Base), jobs=jobs
    )


def _wire_perms(app: FastAPI, perms: Permissions) -> None:
    settings = get_settings()
    app.state.perms = perms
    app.state.subjects = SubjectsCache(perms, settings.perm_cache_ttl_s, settings.perm_stale_max_s)
