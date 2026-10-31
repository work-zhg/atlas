"""授权 = 用户中心数据权限（数据编码属于本服务这个应用）。

    McpServer  一个服务一条，数据标识 = 服务名
               READ   授给 Key：全部工具（含以后新增的）
               OWNER  授给人：所有者，能给 Key 分配这个服务的权限
    McpTool    一个工具一条，数据标识 = 服务名/工具名；第一次工具级授权时才登记
               READ   授给 Key：只能用这个工具
               OWNER  和服务的所有者保持一致（用户中心要求改授权的人是这条数据的 Owner）

★ Key 是权限主体：每把 Key 在用户中心有一个主体，本期复用数字员工（没有密码、不能登录、
  没有部门，部门授权落不到它身上）。网关判权时以 Key 的主体 uid 作 X-Auth-Uid 调用户中心。
  主体停用后用户中心对它一律回「无权限」：删 Key 时就这样让它的授权全部失效。
★ 公开服务不判权，不查用户中心。
★ 判权结果按 Key 短时缓存：每次工具调用都过网关，不能每个请求都查用户中心。新授、收回的授权
  在缓存时长内生效。用户中心不可用、缓存又过期时一律拒绝（Unavailable），不放行。
★ MCP 管理员给不是自己的服务授权，要在用户中心有「全部 MCP 服务 · 所有者」和
  「全部 MCP 工具 · 所有者」（数据编码级授权）：角色只管能不能做这类事，对哪些数据是数据权限。

两个实现：UCPermissions（用户中心开放接口）；测试里的内存实现（tests/gateway_testkit.py）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .auth import outbound_headers
from .errors import Conflict, Forbidden, Invalid, NotFound, Unavailable

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_SERVER",
    "DATA_TOOL",
    "AccessCache",
    "AclEntry",
    "KeyAccess",
    "Permissions",
    "UCPermissions",
    "tool_data_id",
]

#: 数据编码：只表达语义，不带应用名
DATA_SERVER = "McpServer"
DATA_TOOL = "McpTool"
LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


def tool_data_id(service: str, tool: str) -> str:
    return f"{service}/{tool}"


@dataclass(frozen=True)
class KeyAccess:
    """一把 Key 被授了哪些私有服务、哪些工具。

    servers  服务级授权：这些服务的全部工具（含以后新增的）
    tools    工具级授权：「服务名/工具名」
    """

    servers: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()

    def allows(self, service: str, tool: str) -> bool:
        return service in self.servers or tool_data_id(service, tool) in self.tools

    def tools_of(self, service: str) -> frozenset[str]:
        prefix = f"{service}/"
        return frozenset(t[len(prefix) :] for t in self.tools if t.startswith(prefix))

    def touches(self, service: str) -> bool:
        return service in self.servers or bool(self.tools_of(service))


@dataclass(frozen=True)
class AclEntry:
    """一条授权（用户中心的 ACL 行）。"""

    id: str
    level: str
    subject_type: str  # USER | DEPT
    subject_id: str
    subject_name: str = ""


class Permissions(Protocol):
    # ── 判权
    async def key_access(self, subject: str) -> KeyAccess: ...

    async def owned(self, user: str) -> tuple[frozenset[str], bool]:
        """这个人是所有者的服务；第二项 = 有「全部 MCP 服务 · 所有者」。"""
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

    # ── 主体
    async def user(self, uid: str) -> dict[str, Any] | None: ...

    async def search_users(self, q: str) -> list[dict[str, Any]]:
        """选人（只有人，不含数字员工）：添加所有者时用。"""
        ...

    async def create_subject(self, *, ref: str, name: str, owner: str) -> str:
        """为一把 Key 建用户中心主体（幂等：同一个 ref 返回同一个 uid）。→ uid。"""
        ...

    async def set_subject_active(self, uid: str, active: bool) -> None: ...


def _unavailable() -> Unavailable:
    return Unavailable("用户中心暂不可用，暂时无法校验 MCP 服务的授权", code="AUTHZ_UNAVAILABLE")


class UCPermissions:
    """用户中心开放接口（/open/v1/data-permissions*、/open/v1/digital-employees*）。"""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10.0, trust_env=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            logger.warning("用户中心不可达：%s", exc)
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
        if r.status_code == 404:
            raise NotFound(message, code=code or "NOT_FOUND")
        if r.status_code < 500 and r.status_code not in (401, 403):
            raise Invalid(message, code=code or "INVALID")
        logger.warning("用户中心拒绝了开放接口调用：%s %s", r.status_code, r.text[:200])
        raise _unavailable()

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
        # ★ scope=data：Key 只认授给单条数据的（编码级授权是给管理员的，不该落到 Key 上）
        servers, _ = await self._accessible(DATA_SERVER, subject, "READ", scope="data")
        tools, _ = await self._accessible(DATA_TOOL, subject, "READ", scope="data")
        return KeyAccess(servers, tools)

    async def owned(self, user: str) -> tuple[frozenset[str], bool]:
        return await self._accessible(DATA_SERVER, user, "OWNER", scope="all")

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


class AccessCache:
    """「Key 的主体 → 被授了什么」的短时缓存。控制面（替 Atlas 回答）和数据面（每个请求判权）共用。

    ★ 用户中心不可用时：缓存没过期就继续用，过期了一律拒绝（Unavailable 往上抛）。
    """

    def __init__(self, inner: Permissions, ttl_s: float) -> None:
        self.inner = inner
        self._ttl = ttl_s
        self._rows: dict[str, tuple[float, KeyAccess]] = {}

    async def get(self, subject: str) -> KeyAccess:
        row = self._rows.get(subject)
        if row is not None and row[0] >= time.monotonic():
            return row[1]
        found = await self.inner.key_access(subject)
        if self._ttl > 0:
            if len(self._rows) > 50_000:
                self._rows.clear()
            self._rows[subject] = (time.monotonic() + self._ttl, found)
        return found

    def forget(self, subject: str | None = None) -> None:
        """本进程里刚改过授权：立即生效（别的实例等缓存过期）。"""
        if subject is None:
            self._rows.clear()
        else:
            self._rows.pop(subject, None)
