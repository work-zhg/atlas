"""用户中心开放接口的唯一客户端（c4-model/knowledge/code-access.html · UserCenterGateway）。

    授权快照    GET /open/v1/users/{id}/authz
                操作码、角色、所属租户（按 auth-uid + 租户缓存）
    数据权限    /open/v1/data-permissions*
                库级权限，数据编码 AtlasKnowledgeBase（perms.py）
    用户与部门  GET /open/v1/users/{id}、/depts/tree
                主体展开（principals.py）、知识集维护人姓名

以 Atlas 应用的身份调（知识库是 Atlas 的数据，数据编码登记在它名下）：内网免令牌，请求头写
X-Caller-App: atlas，并透传本次调用的身份请求头（auth.outbound_headers）。

错误只有一套口径（原来三类调用分在三个模块，各自映射，规则不完全一致）：
    不可达、5xx、意外的 401 / 403            Unavailable（503）—— 一律不放行
    OWNER_REQUIRED、NOT_DATA_OWNER            Forbidden；LAST_DATA_OWNER → Conflict
    404                                       NotFound（授权快照的 401 也是：这个人不在这个租户里）
    其他 4xx                                  Invalid
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

import httpx
from atlas_tenancy import TENANT_HEADER

from ..errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from .auth import outbound_headers
from .perms import DATA_CODE, Grant

__all__ = ["UserCenterGateway"]

logger = logging.getLogger(__name__)


class _Cache:
    """(auth-uid, 租户) → (过期时刻, 快照)。只缓存成功结果。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, UUID], tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple[str, UUID]) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: tuple[str, UUID], snap: dict[str, Any], ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, snap)


class UserCenterGateway:
    """实现 perms.DataPermissions；另有授权快照、用户与部门。"""

    def __init__(
        self,
        base_url: str,
        *,
        snapshot_ttl_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10.0, trust_env=False, transport=transport
        )
        self._snapshots = _Cache()
        self._ttl = snapshot_ttl_s

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        not_found: tuple[int, ...] = (404,),
        **kw: Any,
    ) -> Any:
        try:
            r = await self._http.request(
                method, path, headers={**outbound_headers(), **(headers or {})}, **kw
            )
        except httpx.HTTPError as exc:
            logger.warning("用户中心不可达：%s %s：%s", method, path, exc)
            raise _unavailable() from exc
        if r.status_code < 400:
            return r.json() if r.content else None
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        code = str(body.get("code", ""))
        message = str(body.get("message") or "用户中心拒绝了请求")
        if code in ("OWNER_REQUIRED", "NOT_DATA_OWNER"):
            raise Forbidden(message)
        if code == "LAST_DATA_OWNER":
            raise Conflict(message)
        if r.status_code in not_found:
            raise NotFound(message)
        if r.status_code < 500 and r.status_code not in (401, 403):
            raise Invalid(message)
        logger.warning("用户中心拒绝了开放接口调用：%s %s", r.status_code, r.text[:200])
        raise _unavailable()

    # ------------------------------------------------------------------ 授权快照

    async def snapshot(self, auth_uid: str, tenant: UUID) -> dict[str, Any]:
        """这个人在这个租户里的授权快照。不在这个租户、停用、不存在 → NotFound。"""
        hit = self._snapshots.get((auth_uid, tenant))
        if hit is not None:
            return hit
        snap: dict[str, Any] = await self._call(
            "GET",
            f"/open/v1/users/{auth_uid}/authz",
            headers={TENANT_HEADER: str(tenant)},
            not_found=(401, 404),
        )
        self._snapshots.put((auth_uid, tenant), snap, self._ttl)
        return snap

    # ------------------------------------------------------------------ 用户与部门

    async def user(self, user_id: str) -> dict[str, Any]:
        return dict(await self._call("GET", f"/open/v1/users/{user_id}"))

    async def roles(self, user_id: str) -> list[str]:
        """Atlas 应用内的角色（授权快照里的，按本次调用的租户）。"""
        snap = await self._call("GET", f"/open/v1/users/{user_id}/authz")
        return [str(r) for r in snap.get("roles") or []]

    async def dept_tree(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/open/v1/depts/tree"))

    async def user_names(self, user_ids: list[str]) -> dict[str, str]:
        """→ {用户 id: 姓名}（知识集维护人显示用）；查不到的不在结果里。"""

        async def one(uid: str) -> tuple[str, str] | None:
            try:
                u = await self.user(uid)
            except NotFound:
                return None
            return uid, str(u.get("name") or u.get("account") or uid)

        found = await asyncio.gather(*(one(u) for u in dict.fromkeys(user_ids)))
        return dict(f for f in found if f)

    # ------------------------------------------------------------------ 数据权限（DataPermissions）

    async def level(self, user_id: str, dataset_id: str) -> str:
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": DATA_CODE, "data_id": dataset_id, "user_id": user_id},
        )
        return str(r["permission"])

    async def accessible(self, user_id: str, min_level: str = "READ") -> tuple[dict[str, str], str]:
        out: dict[str, str] = {}
        page, type_level = 1, "NONE"
        while True:
            r = await self._call(
                "GET",
                "/open/v1/data-permissions/accessible",
                params={
                    "data_code": DATA_CODE,
                    "user_id": user_id,
                    "min": min_level,
                    "page": page,
                    "size": 500,
                },
            )
            type_level = str(r.get("type_permission") or "NONE")
            out.update({i["data_id"]: i["permission"] for i in r["items"]})
            if len(out) >= int(r.get("total") or 0) or not r["items"]:
                return out, type_level
            page += 1

    @staticmethod
    def _grants(grants: list[Grant]) -> list[dict[str, Any]]:
        return [
            {
                "permission": g.level,
                "subject_type": g.subject_type.upper(),
                "subject_id": g.subject_id,
                "include_sub": g.include_sub,
            }
            for g in grants
        ]

    async def create(self, dataset_id: str, name: str, grants: list[Grant]) -> None:
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": DATA_CODE,
                "data_id": dataset_id,
                "data_name": name,
                "grants": self._grants(grants),
            },
        )

    async def acl(self, dataset_id: str) -> list[dict[str, Any]]:
        try:
            return list(
                await self._call(
                    "GET",
                    "/open/v1/data-permissions/acl",
                    params={"data_code": DATA_CODE, "data_id": dataset_id},
                )
            )
        except NotFound:
            return []

    async def grant(self, dataset_id: str, operator: str, grants: list[Grant]) -> None:
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": DATA_CODE,
                "data_id": dataset_id,
                "operator_id": operator,
                "grants": self._grants(grants),
            },
        )

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        body: dict[str, Any] = {"operator_id": operator}
        if level is not None:
            body["permission"] = level
        if include_sub is not None:
            body["include_sub"] = include_sub
        await self._call("PATCH", f"/open/v1/data-permissions/acl/{acl_id}", json=body)

    async def revoke(self, acl_id: str, operator: str) -> None:
        await self._call(
            "DELETE", f"/open/v1/data-permissions/acl/{acl_id}", params={"operator_id": operator}
        )

    async def rename(self, dataset_id: str, name: str) -> None:
        try:
            await self._call(
                "PATCH",
                "/open/v1/data-objects",
                json={"data_code": DATA_CODE, "data_id": dataset_id, "data_name": name},
            )
        except NotFound:  # 还没登记：无事可做
            return

    async def cleanup(self, dataset_id: str) -> None:
        await self._call(
            "DELETE",
            "/open/v1/data-permissions",
            params={"data_code": DATA_CODE, "data_id": dataset_id},
        )


def _unavailable() -> Unavailable:
    return Unavailable("用户中心暂不可用，请稍后重试")
