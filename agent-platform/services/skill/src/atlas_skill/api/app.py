from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from atlas_tenancy import TenantScope
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..access.control import AccessControl
from ..access.usercenter import UserCenterGateway
from ..check.package import ScanBlocked
from ..errors import SkillError
from ..settings import get_settings
from . import admin, internal
from .ops import mount_ops

__all__ = ["create_app"]


def create_app(
    *,
    storage: Any = None,
    uc: UserCenterGateway | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """storage、uc 可注入 —— 测试用 moto 桶、接替身的用户中心网关；不注入则按设置构造。"""
    settings = get_settings()
    owned = uc is None
    gateway = uc or UserCenterGateway(
        settings.uc_base_url, app_code=settings.uc_app_code, snapshot_ttl_s=settings.authz_ttl_s
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned:
                await gateway.aclose()

    app = FastAPI(title="Atlas Skill", version="0.1.0", lifespan=lifespan)
    app.state.access = AccessControl(gateway)
    if storage is not None:
        app.state.storage = storage
    if cors_origins:
        # BFF 上线前，管理页由浏览器直连（设计 §13.3）
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(SkillError)
    async def _skill_error(_: Request, exc: SkillError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, **({"context": exc.detail} if exc.detail else {})},
        )

    @app.exception_handler(ScanBlocked)
    async def _blocked(_: Request, exc: ScanBlocked) -> JSONResponse:
        # ★ 400 而不是 422：包本身不合格，不是请求格式错。命中项原样给出，
        #   上传者据此改包，不必去翻服务端日志。
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "layer": exc.layer, "hits": exc.hits},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(internal.router)
    app.include_router(admin.router)
    # 运营接口：技能市场，租户的停用、恢复、删除
    mount_ops(app)
    # 每个请求从「没有租户」开始，由 deps.verified_tenant 进入
    app.add_middleware(_TenantIsolation)
    return app


class _TenantIsolation:
    """纯 ASGI：每个请求一个干净的租户上下文。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        with TenantScope.isolated():
            await self.app(scope, receive, send)
