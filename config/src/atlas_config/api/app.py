from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..errors import ConfigError
from ..skill.package import ScanBlocked
from . import admin, internal

__all__ = ["create_app"]


def create_app(*, storage: Any = None, cors_origins: list[str] | None = None) -> FastAPI:
    """storage 可注入 —— 测试用 moto 桶；不注入则首次用到时按设置构造。"""
    app = FastAPI(title="Atlas Config", version="0.1.0")
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

    @app.exception_handler(ConfigError)
    async def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
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
    return app
