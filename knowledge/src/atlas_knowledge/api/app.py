from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..errors import KnowledgeError
from ..runtime import Runtime, build_runtime
from ..settings import get_settings
from ..storage import StorageNotConfigured
from . import datasets, internal
from .mcp import build_mcp

__all__ = ["create_app"]

logger = logging.getLogger(__name__)


def create_app(*, runtime: Runtime | None = None, cors_origins: list[str] | None = None) -> FastAPI:
    """runtime 可注入 —— 测试用内存 Qdrant、假模型、内联投递。"""

    mcp = build_mcp(lambda: app.state.runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        app.state.runtime = runtime or build_runtime(get_settings())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if owned:
                await app.state.runtime.aclose()

    app = FastAPI(title="Atlas Knowledge", version="0.1.0", lifespan=lifespan)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
    # Pod 里的 ACP CLI 经它调 knowledge_search（令牌鉴权，见 api/mcp.py）
    app.mount("/mcp", mcp.streamable_http_app())
    return app
