"""用户中心开放接口客户端（/open/v1）。Atlas 与用户中心的唯一接触点（权限设计 §4、§5）。

★ 应用身份：App Key / Secret 换接口令牌，本地缓存、提前 5 分钟续期；令牌失效（401）时重换一次。
★ 测试里用内存实现替换，业务代码只依赖这里的方法签名。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import httpx

from ..config import Settings
from ..errors import AppError

__all__ = ["UCClient", "UCError"]


class UCError(AppError):
    """用户中心返回的业务错误，或用户中心不可用。"""

    status_code = 502

    def __init__(self, kind: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


class UCClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        # 用户中心在内网 / 本机：不走代理
        self._http = httpx.AsyncClient(
            base_url=settings.uc_base_url, timeout=10.0, transport=transport, trust_env=False
        )
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _app_token(self, *, refresh: bool = False) -> str:
        async with self._lock:
            if not refresh and self._token and time.monotonic() < self._token_exp - 300:
                return self._token
            if not self.settings.uc_app_key:
                raise UCError("uc_not_configured", "未配置用户中心 App Key（UC_APP_KEY）", 503)
            try:
                r = await self._http.post(
                    "/open/v1/auth/token",
                    json={
                        "app_key": self.settings.uc_app_key,
                        "app_secret": self.settings.uc_app_secret.get_secret_value(),
                    },
                )
            except httpx.HTTPError as exc:
                raise UCError("uc_unavailable", "用户中心暂不可用，请稍后重试", 503) from exc
            if r.status_code != 200:
                raise UCError("uc_auth_failed", "Atlas 在用户中心的应用凭据无效", 503)
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.monotonic() + int(body["expires_in"])
            return self._token

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in (0, 1):
            token = await self._app_token(refresh=attempt == 1)
            try:
                r = await self._http.request(
                    method, path, headers={"Authorization": f"Bearer {token}"}, **kw
                )
            except httpx.HTTPError as exc:
                raise UCError("uc_unavailable", "用户中心暂不可用，请稍后重试", 503) from exc
            if r.status_code == 401 and attempt == 0:
                try:
                    if r.json().get("code") in ("INVALID_TOKEN", "APP_DISABLED"):
                        continue
                except ValueError:
                    pass
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    raise UCError("uc_error", f"用户中心返回 {r.status_code}") from None
                status = r.status_code if r.status_code in (401, 403, 404, 409, 422) else 502
                raise UCError(
                    str(body.get("code", "uc_error")).lower(),
                    str(body.get("message", "用户中心返回错误")),
                    status,
                )
            return r.json()
        raise UCError("uc_auth_failed", "Atlas 在用户中心的应用凭据无效", 503)

    async def verify_password(self, account: str, password: str) -> dict[str, Any]:
        """→ {user: {id, account, name, email, status, dept: {id, path}}, can_access, roles,
        permissions, menus}"""
        return await self._call(
            "POST", "/open/v1/auth/verify-password", json={"account": account, "password": password}
        )

    async def authz(self, user_id: UUID) -> dict[str, Any]:
        """授权快照：{can_access, roles, permissions, menus}（不在应用可访问范围时全空）。"""
        return await self._call("GET", f"/open/v1/users/{user_id}/authz")

    async def user(self, user_id: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/users/{user_id}")
