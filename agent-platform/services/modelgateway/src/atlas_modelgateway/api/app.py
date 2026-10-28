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
from ..litellm_api import LiteLLM
from ..perms import Permissions, SubjectsCache, UCPermissions
from ..reconcile import run_reconciler
from ..registry import DeploymentsCache
from ..settings import get_settings
from . import admin, internal

__all__ = ["create_app"]


def create_app(
    *,
    perms: Permissions | None = None,
    litellm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """perms、litellm_transport 可注入 —— 测试用内存实现；不注入则按设置连用户中心、LiteLLM。"""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: UCPermissions | None = None
        if perms is None:
            owned = UCPermissions(settings.uc_base_url)
            _wire_perms(app, owned)
        litellm = LiteLLM(
            settings.litellm_base_url,
            settings.litellm_master_key.get_secret_value() if settings.litellm_master_key else None,
            timeout_s=settings.litellm_timeout_s,
            transport=litellm_transport,
        )
        app.state.litellm = litellm
        app.state.deployments = DeploymentsCache(litellm, settings.litellm_models_ttl_s)
        reconciler = (
            asyncio.create_task(
                run_reconciler(
                    settings.reconcile_interval_s,
                    get_sessionmaker(),
                    litellm,
                    app.state.deployments,
                    app.state.perms,
                    app.state.subjects,
                )
            )
            if settings.reconcile_interval_s > 0
            else None
        )
        try:
            yield
        finally:
            if reconciler is not None:
                reconciler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reconciler
            await litellm.aclose()
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="Model Management", version="0.2.0", lifespan=lifespan)
    if perms is not None:
        _wire_perms(app, perms)
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

    app.include_router(admin.router)
    app.include_router(internal.router)
    return app


def _wire_perms(app: FastAPI, perms: Permissions) -> None:
    settings = get_settings()
    app.state.perms = perms
    app.state.subjects = SubjectsCache(perms, settings.perm_cache_ttl_s, settings.perm_stale_max_s)
