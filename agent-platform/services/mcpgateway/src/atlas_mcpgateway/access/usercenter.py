"""UserCenterGateway：网关里唯一调用户中心的地方（防腐层，c4-model/mcpgateway/code-access.html）。

    数据权限    /open/v1/data-permissions*、/open/v1/data-objects
                McpServer 一个服务一条（数据标识 = 服务名）：READ 授给 Key = 全部工具，
                OWNER 授给人 = 所有者；McpTool 一个工具一条（服务名/工具名）：READ = 只能用这个工具
    数字员工    /open/v1/digital-employees*：每把 Key 在用户中心的权限主体
    用户        /open/v1/users*：查人、选人
    授权快照    /open/v1/users/{id}/authz：管理端的操作码、所属租户（按 auth-uid + 租户缓存）

★ 带上本次请求的身份请求头（access/identity.py）；数据面没有入站身份时用 Key 的主体；
  X-Caller-App 写 mcp-gateway。
★ 错误只有一份口径（授权快照、数据权限、数字员工、用户都走 _call）：
      OWNER_REQUIRED、NOT_DATA_OWNER → Forbidden；LAST_DATA_OWNER → Conflict
      404                             → NotFound（授权快照的 401、404 → Unauthorized）
      其他 4xx                         → Invalid
      不可达、5xx、意外的 401 / 403     → Unavailable（503）—— 一律不放行

两个实现：UserCenterGateway；测试里的内存实现（tests/gateway_testkit.py 的 FakePermissions）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import httpx
from atlas_tenancy import TENANT_HEADER, parse_tenant

from ..db.models import DATA_SERVER, DATA_TOOL
from ..errors import Conflict, Forbidden, Invalid, NotFound, Unauthorized, Unavailable
from .identity import outbound_headers
from .key_access import KeyAccess

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_SERVER",
    "DATA_TOOL",
    "AclEntry",
    "Snapshot",
    "UserCenter",
    "UserCenterGateway",
    "key_access_from",
    "tool_data_id",
]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


def tool_data_id(service: str, tool: str) -> str:
    return f"{service}/{tool}"


def key_access_from(servers: Iterable[str], tool_ids: Iterable[str]) -> KeyAccess:
    """用户中心给的数据标识 → KeyAccess（工具按服务分组）。"""
    grouped: dict[str, set[str]] = {}
    for data_id in tool_ids:
        service, sep, tool = data_id.partition("/")
        if sep and tool:
            grouped.setdefault(service, set()).add(tool)
    return KeyAccess(frozenset(servers), {s: frozenset(t) for s, t in grouped.items()})


@dataclass(frozen=True)
class AclEntry:
    """一条授权（用户中心的 ACL 行）。"""

    id: str
    level: str
    subject_type: str  # USER | DEPT
    subject_id: str
    subject_name: str = ""


@dataclass(frozen=True)
class Snapshot:
    """授权快照：这个人在这个租户里的操作码，以及用户中心给的所属租户。"""

    operations: frozenset[str]
    tenant: UUID | None


class UserCenter(Protocol):
    # ── 判权
    async def key_access(self, subject: str) -> KeyAccess:
        """只认授给单条数据的（scope=data）：编码级授权是给管理员的，不该落到 Key 上。"""
        ...

    async def owned(self, user: str) -> tuple[frozenset[str], bool]:
        """这个人是所有者的服务；第二项 = 有「全部 MCP 服务 · 所有者」。"""
        ...

    async def authz(self, uid: str, tenant: UUID) -> Snapshot:
        """不在这个租户里、停用、不存在 → Unauthorized。"""
        ...

    # ── 数据对象与授权
    async def register(
        self,
        data_code: str,
        data_id: str,
        name: str,
        *,
        owners: list[str],
        readers: list[str] | None = None,
    ) -> None:
        """登记数据对象并写第一批授权（不需要操作人）。已经有授权则 Conflict。"""
        ...

    async def rename(self, data_code: str, data_id: str, name: str) -> None: ...

    async def drop(self, data_code: str, data_id: str) -> None:
        """删掉数据对象和它的全部授权（没有就算了）。"""
        ...

    async def acl(self, data_code: str, data_id: str) -> list[AclEntry]: ...

    async def grant(
        self, data_code: str, data_id: str, operator: str, *, level: str, users: list[str]
    ) -> None:
        """给用户（含 Key 主体）授权。操作人要是这条数据的 Owner（用户中心校验）。"""
        ...

    async def revoke(self, acl_id: str, operator: str) -> None: ...

    # ── 用户与主体
    async def user(self, uid: str) -> dict[str, Any] | None: ...

    async def search_users(self, q: str) -> list[dict[str, Any]]:
        """选人（只有人，不含数字员工）：添加所有者时用。"""
        ...

    async def user_by_account(self, account: str) -> dict[str, Any] | None: ...

    async def create_subject(self, *, ref: str, name: str, owner: str) -> str:
        """为一把 Key 建用户中心主体（幂等：同一个 ref 返回同一个 uid）。→ uid。"""
        ...

    async def set_subject_active(self, uid: str, active: bool) -> None: ...


def _unavailable() -> Unavailable:
    return Unavailable("用户中心暂不可用，请稍后重试", code="AUTHZ_UNAVAILABLE")


class _SnapshotCache:
    """(auth-uid, 租户) → (过期时刻, 授权快照)。只缓存成功结果。"""

    def __init__(self, ttl_s: float) -> None:
        self._ttl = ttl_s
        self._rows: dict[tuple[str, UUID], tuple[float, Snapshot]] = {}

    def get(self, key: tuple[str, UUID]) -> Snapshot | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: tuple[str, UUID], snap: Snapshot) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + self._ttl, snap)


class UserCenterGateway:
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
        self._snapshots = _SnapshotCache(snapshot_ttl_s)

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
            raise Forbidden(message, code=code)
        if code == "LAST_DATA_OWNER":
            raise Conflict(message, code=code)
        if r.status_code in not_found:
            raise NotFound(message, code=code or "NOT_FOUND")
        if r.status_code < 500 and r.status_code not in (401, 403):
            raise Invalid(message, code=code or "INVALID")
        logger.warning("用户中心拒绝了开放接口调用：%s %s", r.status_code, r.text[:200])
        raise _unavailable()

    # ------------------------------------------------------------------ 判权

    async def _accessible(
        self, data_code: str, user: str, min_level: str, *, scope: str
    ) -> tuple[frozenset[str], bool]:
        names: set[str] = set()
        page = 1
        while True:
            try:
                r = await self._call(
                    "GET",
                    "/open/v1/data-permissions/accessible",
                    params={
                        "data_code": data_code,
                        "user_id": user,
                        "min": min_level,
                        "scope": scope,
                        "page": page,
                        "size": 500,
                    },
                )
            except NotFound:
                # 用户中心里没有这个主体（或编码还没登记）：没有任何权限
                return frozenset(), False
            items = r.get("items") or []
            names.update(str(i["data_id"]) for i in items)
            if len(names) >= int(r.get("total") or 0) or not items:
                level = str(r.get("type_permission") or "NONE")
                return frozenset(names), LEVEL_RANK.get(level, 0) >= LEVEL_RANK[min_level]
            page += 1

    async def key_access(self, subject: str) -> KeyAccess:
        servers, _ = await self._accessible(DATA_SERVER, subject, "READ", scope="data")
        tools, _ = await self._accessible(DATA_TOOL, subject, "READ", scope="data")
        return key_access_from(servers, tools)

    async def owned(self, user: str) -> tuple[frozenset[str], bool]:
        return await self._accessible(DATA_SERVER, user, "OWNER", scope="all")

    async def authz(self, uid: str, tenant: UUID) -> Snapshot:
        hit = self._snapshots.get((uid, tenant))
        if hit is not None:
            return hit
        try:
            body = await self._call(
                "GET",
                f"/open/v1/users/{uid}/authz",
                headers={TENANT_HEADER: str(tenant)},
                not_found=(401, 404),
            )
        except NotFound as exc:
            raise Unauthorized("请先登录") from exc
        user = body.get("user") or {}
        if user.get("status") == "disabled":
            raise Unauthorized("账号已停用")
        ops = frozenset(body.get("permissions") or []) if body.get("can_access") else frozenset()
        snap = Snapshot(ops, parse_tenant(user.get("tenant_id")))
        self._snapshots.put((uid, tenant), snap)
        return snap

    # ------------------------------------------------------------------ 数据对象与授权

    async def register(
        self,
        data_code: str,
        data_id: str,
        name: str,
        *,
        owners: list[str],
        readers: list[str] | None = None,
    ) -> None:
        grants = [{"permission": "OWNER", "subject_type": "USER", "subject_id": u} for u in owners]
        grants += [
            {"permission": "READ", "subject_type": "USER", "subject_id": u} for u in readers or []
        ]
        try:
            await self._call(
                "POST",
                "/open/v1/data-permissions/batch",
                json={
                    "data_code": data_code,
                    "data_id": data_id,
                    "data_name": (name or data_id)[:100],
                    "grants": grants,
                },
            )
        except Forbidden as exc:
            # OWNER_REQUIRED：这条数据已经有授权
            raise Conflict(
                f"用户中心里已经登记过 {data_code} {data_id}", code="UC_REGISTERED"
            ) from exc

    async def rename(self, data_code: str, data_id: str, name: str) -> None:
        try:
            await self._call(
                "PATCH",
                "/open/v1/data-objects",
                json={"data_code": data_code, "data_id": data_id, "data_name": name[:100]},
            )
        except NotFound:
            return

    async def drop(self, data_code: str, data_id: str) -> None:
        try:
            await self._call(
                "DELETE",
                "/open/v1/data-permissions",
                params={"data_code": data_code, "data_id": data_id},
            )
        except NotFound:
            return

    async def acl(self, data_code: str, data_id: str) -> list[AclEntry]:
        try:
            rows = await self._call(
                "GET",
                "/open/v1/data-permissions/acl",
                params={"data_code": data_code, "data_id": data_id},
            )
        except NotFound:
            return []
        return [
            AclEntry(
                id=str(r["id"]),
                level=str(r["permission"]),
                subject_type=str(r["subject"]["type"]),
                subject_id=str(r["subject"]["id"]),
                subject_name=str(r["subject"].get("name") or ""),
            )
            for r in rows or []
        ]

    async def grant(
        self, data_code: str, data_id: str, operator: str, *, level: str, users: list[str]
    ) -> None:
        if not users:
            return
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": data_code,
                "data_id": data_id,
                "operator_id": operator,
                "grants": [
                    {"permission": level, "subject_type": "USER", "subject_id": u} for u in users
                ],
            },
        )

    async def revoke(self, acl_id: str, operator: str) -> None:
        await self._call(
            "DELETE", f"/open/v1/data-permissions/acl/{acl_id}", params={"operator_id": operator}
        )

    # ------------------------------------------------------------------ 用户与主体

    async def user(self, uid: str) -> dict[str, Any] | None:
        try:
            return dict(await self._call("GET", f"/open/v1/users/{uid}"))
        except NotFound:
            return None

    async def search_users(self, q: str) -> list[dict[str, Any]]:
        body = await self._call(
            "GET", "/open/v1/users", params={"q": q, "types": "human", "size": 20}
        )
        return list((body or {}).get("items") or [])

    async def user_by_account(self, account: str) -> dict[str, Any] | None:
        try:
            return dict(await self._call("GET", f"/open/v1/users/by-account/{account}"))
        except NotFound:
            return None

    async def create_subject(self, *, ref: str, name: str, owner: str) -> str:
        body = await self._call(
            "POST",
            "/open/v1/digital-employees",
            json={
                "external_ref": ref,
                "name": name[:32],
                "owner_id": owner,
                "description": "MCP 网关的 API Key（授权授给它）",
            },
        )
        return str(body["id"])

    async def set_subject_active(self, uid: str, active: bool) -> None:
        action = "enable" if active else "disable"
        try:
            await self._call("POST", f"/open/v1/digital-employees/{uid}/{action}")
        except NotFound:
            return
