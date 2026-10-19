"""用户中心开放接口客户端（/open/v1）。Atlas 与用户中心的唯一接触点（权限设计 §4、§5）。

★ 内网免令牌（内网身份传递 §3、§11）：应用身份 = 请求头 X-Caller-App: atlas；租户与「谁在用」
  原样透传本次调用的 X-Tenant-Id / X-Uid（outbound.py）。用户中心按它们建立租户上下文。
★ 测试里用内存实现替换，业务代码只依赖这里的方法签名。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from ..config import Settings
from ..errors import AppError
from ..outbound import outbound_headers

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

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            raise UCError("uc_unavailable", "用户中心暂不可用，请稍后重试", 503) from exc
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

    async def authz(self, user_id: UUID) -> dict[str, Any]:
        """授权快照：{can_access, roles, permissions, menus}（不在应用可访问范围时全空）。"""
        return await self._call("GET", f"/open/v1/users/{user_id}/authz")

    async def data_type_level(self, user_id: UUID, data_code: str) -> dict[str, Any]:
        """数据编码级授权：{permission, sources} —— 用户对该编码下全部数据的级别。"""
        return await self._call(
            "GET",
            "/open/v1/data-permissions/type",
            params={"data_code": data_code, "user_id": str(user_id)},
        )

    async def user(self, user_id: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/users/{user_id}")

    async def search_users(self, q: str | None, *, size: int = 20) -> list[dict[str, Any]]:
        """选人：本应用可访问范围内、未停用的用户 → [{id, account, name, dept_id}]。"""
        r = await self._call("GET", "/open/v1/users", params={"q": q or "", "size": size})
        return list(r["items"])

    async def depts_tree(self) -> list[dict[str, Any]]:
        """→ [{id, parent_id, name, leader_id}]（全部部门，按层级排序）"""
        return await self._call("GET", "/open/v1/depts/tree")
