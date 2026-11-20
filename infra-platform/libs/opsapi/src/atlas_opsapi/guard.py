"""OpsGuard：核对调用方确实是运营中心。

★ 只读接口也要验：各租户的用量、系统参数同样只给运营中心。
★ 没配运营密钥 = 运营接口没启用，一律 403，而不是放行。
★ 同时实现应用配置库的 ConfigGuard、定时任务库的 JobGuard：系统参数、系统任务两组接口直接复用
  两个库的管理接口，只换守卫。返回的操作人是运营人员，记进修订历史。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from .signature import SignatureError, verify
from .stamp import OperatorStamp

__all__ = ["OpsGuard"]


class OpsGuard:
    def __init__(self, key: bytes | str | None, *, window: int = 300) -> None:
        self._key = key.encode() if isinstance(key, str) else key
        self._window = window

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def verify(self, request: Request) -> OperatorStamp:
        if not self._key:
            raise HTTPException(403, "运营接口没有启用（没有配置运营密钥）")
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        body = await request.body()
        try:
            verify(self._key, request.method, path, body, request.headers, window=self._window)
        except SignatureError as exc:
            raise HTTPException(401, f"不是运营中心的请求：{exc}") from exc
        return OperatorStamp.from_headers(request.headers)

    # ── ConfigGuard / JobGuard

    async def view(self, request: Request) -> str:
        return (await self.verify(request)).who

    async def edit(self, request: Request) -> str:
        return (await self.verify(request)).who

    def dependency(self) -> Callable[..., Any]:
        """FastAPI 依赖：宿主自己的运营接口（Atlas 的份额、沙箱模板）挂上它，拿到 OperatorStamp。"""

        async def _verify(request: Request) -> OperatorStamp:
            return await self.verify(request)

        return _verify
