from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from atlas_opsapi import OpsGuard, OpsRoutes, TenantLifecycle
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..auth import IdentityHeadersMiddleware
from ..db.models import Base, Entry, Instance, Revision, Topic
from ..db.session import get_sessionmaker
from ..errors import MemoryServiceError
from ..perms import CachedPermissions, DataPermissions, UCDataPermissions
from ..settings import get_settings
from . import instances, scopes

__all__ = ["create_app"]


def create_app(*, perms: DataPermissions | None = None) -> FastAPI:
    """perms 可注入 —— 测试用内存实现；不注入则按设置连用户中心。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UCDataPermissions | None = None
        if perms is None:
            settings = get_settings()
            owned = UCDataPermissions(settings.uc_base_url)
            app.state.perms = CachedPermissions(owned, settings.perm_cache_ttl_s)
        try:
            yield
        finally:
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="Atlas Memory", version="0.1.0", lifespan=lifespan)
    if perms is not None:
        # 注入的实现不依赖生命周期（测试用 ASGITransport 时 lifespan 不会跑）
        app.state.perms = perms
    # 身份请求头透传：本请求里调用户中心都带上（auth.py）
    app.add_middleware(IdentityHeadersMiddleware)

    @app.exception_handler(MemoryServiceError)
    async def _error(_: Request, exc: MemoryServiceError) -> JSONResponse:
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

    app.include_router(scopes.router)
    app.include_router(instances.router)
    _mount_ops(app)
    return app


class MemoryTenantLifecycle(TenantLifecycle):
    """记忆服务的租户接入。新租户不用准备什么；删除只有数据库里的行（没有外部资源），
    用户中心里登记的数据权限随租户在用户中心里一起删。"""

    purge_order = (Revision, Entry, Topic, Instance)

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        return None


def _mount_ops(app: FastAPI) -> None:
    """运营接口（/ops/v1）：租户的创建、删除，作用域定义（api/scopes.py）。"""
    settings = get_settings()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    app.state.ops_guard = guard = OpsGuard(key)
    OpsRoutes.mount(app, guard, MemoryTenantLifecycle(get_sessionmaker(), base=Base))
