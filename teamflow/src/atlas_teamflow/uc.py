"""用户中心开放接口客户端（/open/v1）。TeamFlow 与用户中心的唯一接触点。

★ 应用身份：App Key / Secret 换接口令牌（2 小时），本地缓存、提前 5 分钟续期；
  令牌失效（401）时重换一次。
★ 测试里用内存实现替换（tests/fakes.py），业务代码只依赖这里的方法签名。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import httpx

from .errors import Forbidden, Invalid, NotFound, TFError, Unauthorized, Upstream
from .settings import TFSettings

__all__ = ["TEAM_CODE", "UCClient"]

#: 团队数据权限的数据编码（权限设计 §07）
TEAM_CODE = "Team"

_PASS_THROUGH = {401: Unauthorized, 403: Forbidden, 404: NotFound, 409: TFError, 422: Invalid}


class UCClient:
    def __init__(
        self, settings: TFSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.uc_base_url, timeout=10.0, transport=transport
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
                raise Upstream("UC_NOT_CONFIGURED", "未配置用户中心 App Key（ATLAS_TF_UC_APP_KEY）")
            try:
                r = await self._http.post(
                    "/open/v1/auth/token",
                    json={
                        "app_key": self.settings.uc_app_key,
                        "app_secret": self.settings.uc_app_secret.get_secret_value(),
                    },
                )
            except httpx.HTTPError as exc:
                raise Upstream("UC_UNAVAILABLE", "用户中心暂不可用，请稍后重试") from exc
            if r.status_code != 200:
                raise Upstream("UC_AUTH_FAILED", "TeamFlow 在用户中心的应用凭据无效")
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
                raise Upstream("UC_UNAVAILABLE", "用户中心暂不可用，请稍后重试") from exc
            if (
                r.status_code == 401
                and attempt == 0
                and r.json().get("code") in ("INVALID_TOKEN", "APP_DISABLED")
            ):
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    raise Upstream("UC_ERROR", f"用户中心返回 {r.status_code}") from None
                cls = _PASS_THROUGH.get(r.status_code, Upstream)
                err = cls(
                    body.get("code", "UC_ERROR"),
                    body.get("message", "用户中心返回错误"),
                    **(body.get("details") or {}),
                )
                if r.status_code == 409:
                    err.status_code = 409
                raise err
            return r.json()
        raise Upstream("UC_AUTH_FAILED", "TeamFlow 在用户中心的应用凭据无效")

    # ───────────────────────────── 身份与授权快照

    async def verify_password(self, account: str, password: str) -> dict[str, Any]:
        """→ {user: {...}, can_access, roles, permissions, menus}"""
        return await self._call(
            "POST", "/open/v1/auth/verify-password", json={"account": account, "password": password}
        )

    async def authz(self, user_uuid: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/users/{user_uuid}/authz")

    async def check_op(self, user_uuid: UUID, permission: str) -> bool:
        r = await self._call(
            "POST",
            "/open/v1/authz/check",
            json={"user_id": str(user_uuid), "permission": permission},
        )
        return bool(r["allowed"])

    async def user(self, user_uuid: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/users/{user_uuid}")

    async def search_users(self, q: str | None, *, size: int = 20) -> list[dict[str, Any]]:
        r = await self._call("GET", "/open/v1/users", params={"q": q or "", "size": size})
        return list(r["items"])

    async def depts(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/open/v1/depts/tree"))

    # ───────────────────────────── 团队数据权限

    async def team_level(self, team_uuid: UUID, user_uuid: UUID) -> str:
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": TEAM_CODE, "data_id": str(team_uuid), "user_id": str(user_uuid)},
        )
        return str(r["permission"])

    async def accessible_teams(self, user_uuid: UUID, min_level: str = "WRITE") -> dict[str, str]:
        """→ {团队 uuid 字符串: 级别}"""
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/accessible",
            params={
                "data_code": TEAM_CODE,
                "user_id": str(user_uuid),
                "min": min_level,
                "size": 500,
            },
        )
        return {i["data_id"]: i["permission"] for i in r["items"]}

    async def write_team_grants(
        self, team_uuid: UUID, team_name: str, grants: list[dict[str, Any]], operator: UUID | None
    ) -> dict[str, Any]:
        """grants: [{permission, subject_type: USER|DEPT, subject_id, include_sub}]"""
        body: dict[str, Any] = {
            "data_code": TEAM_CODE,
            "data_id": str(team_uuid),
            "data_name": team_name,
            "grants": grants,
        }
        if operator:
            body["operator_id"] = str(operator)
        return await self._call("POST", "/open/v1/data-permissions/batch", json=body)

    async def team_acl(self, team_uuid: UUID) -> list[dict[str, Any]]:
        return list(
            await self._call(
                "GET",
                "/open/v1/data-permissions/acl",
                params={"data_code": TEAM_CODE, "data_id": str(team_uuid)},
            )
        )

    async def team_holders(self, team_uuid: UUID, min_level: str = "WRITE") -> list[dict[str, Any]]:
        return list(
            await self._call(
                "GET",
                "/open/v1/data-permissions/holders",
                params={"data_code": TEAM_CODE, "data_id": str(team_uuid), "min": min_level},
            )
        )

    async def update_team_grant(
        self,
        acl_id: str,
        operator: UUID,
        *,
        permission: str | None = None,
        include_sub: bool | None = None,
    ) -> None:
        body: dict[str, Any] = {"operator_id": str(operator)}
        if permission:
            body["permission"] = permission
        if include_sub is not None:
            body["include_sub"] = include_sub
        await self._call("PATCH", f"/open/v1/data-permissions/acl/{acl_id}", json=body)

    async def delete_team_grant(self, acl_id: str, operator: UUID) -> None:
        await self._call(
            "DELETE",
            f"/open/v1/data-permissions/acl/{acl_id}",
            params={"operator_id": str(operator)},
        )

    async def rename_team(self, team_uuid: UUID, name: str) -> None:
        await self._call(
            "PATCH",
            "/open/v1/data-objects",
            json={"data_code": TEAM_CODE, "data_id": str(team_uuid), "data_name": name},
        )
