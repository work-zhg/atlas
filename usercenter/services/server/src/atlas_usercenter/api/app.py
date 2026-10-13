from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..errors import UCError
from . import apps, auth, open, org, perm, users

__all__ = ["create_app"]


def create_app(*, cors_origins: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="Atlas User Center", version="0.1.0")
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(UCError)
    async def _uc_error(_: Request, exc: UCError) -> JSONResponse:
        # 统一错误格式：{code, message, details}（总体设计 §10）
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

    for r in (auth.router, org.router, users.router, apps.router, perm.router, open.router):
        app.include_router(r)
    return app
