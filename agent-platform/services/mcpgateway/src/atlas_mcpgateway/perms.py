"""服务授权 = 用户中心数据权限（数据编码 McpServer，属于本服务这个应用，§5.3）。

一个 MCP 服务是一条数据，数据标识就是服务名。级别：
    READ   「使用」：能在 Agent 里选、能经网关调用
    OWNER  「所有者」：能给人、部门、数字员工授权和收回；注册人自动成为所有者
    WRITE  不用：服务定义由 MCP 管理的操作权限管，不按单条数据分

主体能用哪些服务由用户中心计算（直接授给本人 ∪ 授给所在部门 ∪「全部 MCP 服务」这类编码级授权；
数字员工只认直接授给它的）。

★ 用户中心只允许数据编码的所属应用登记和查询，所以 Atlas 不直接查，问本服务（internal 接口）。
★ 判权结果短时缓存（Q15）：每次工具调用都经过网关，不能每个请求都查用户中心。缓存的是
  「主体 → 能用的服务」整个集合，收回、新授的授权都在缓存时长内生效。
★ 用户中心不可用、缓存又过期时一律拒绝（Unavailable），不放行（§5.8）。

两个实现：UCPermissions（用户中心）；测试里的内存实现（tests/gateway_testkit.py）。
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
    "DATA_CODE",
    "LEVEL_RANK",
    "Grant",
    "Permissions",
    "SubjectServices",
    "UCPermissions",
    "UsableCache",
]

#: 数据编码：只表达语义，不带应用名
DATA_CODE = "McpServer"
LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


@dataclass(frozen=True)
class Grant:
    level: str  # READ | OWNER
    subject_type: str  # user | dept（数字员工也是 user）
    subject_id: str
    include_sub: bool = False


@dataclass(frozen=True)
class SubjectServices:
    """主体能用（≥ READ）的服务。all_services = 有「全部 MCP 服务」这类编码级授权。"""

    names: frozenset[str]
    all_services: bool = False

    def allows(self, service: str) -> bool:
        return self.all_services or service in self.names


class Permissions(Protocol):
    async def usable(self, subject: str) -> SubjectServices: ...

    async def level(self, user_id: str, service: str) -> str: ...

    async def owned(self, user_id: str) -> SubjectServices:
        """级别为 OWNER 的服务（管理端「我是所有者的服务」）。"""
        ...

    async def register(self, service: str, display_name: str, owner: str) -> None:
        """登记数据对象，注册人成为所有者。已登记过（有授权）则冲突。"""
        ...

    async def rename(self, service: str, display_name: str) -> None: ...

    async def acl(self, service: str) -> list[dict[str, Any]]: ...

    async def grant(self, service: str, operator: str, grants: list[Grant]) -> None: ...

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None: ...

    async def revoke(self, acl_id: str, operator: str) -> None: ...

    async def holders(self, service: str) -> list[dict[str, Any]]:
        """实际持有「使用」的人（含经部门、编码级授权得来的），用户中心展开。"""
        ...

    async def user(self, user_id: str) -> dict[str, Any] | None:
        """用户中心里的用户（人或数字员工）；不存在为 None。"""
        ...


def _unavailable() -> Unavailable:
    return Unavailable("用户中心暂不可用，暂时无法校验 MCP 服务的授权", code="AUTHZ_UNAVAILABLE")


class UCPermissions:
    """用户中心开放接口（/open/v1/data-permissions*）。"""

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

    async def _accessible(self, user_id: str, min_level: str) -> SubjectServices:
        names: set[str] = set()
        page = 1
        while True:
            try:
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
            except NotFound:
                # 用户中心里没有这个用户（或编码还没登记）：没有任何权限
                return SubjectServices(frozenset())
            type_level = str(r.get("type_permission") or "NONE")
            items = r.get("items") or []
            names.update(str(i["data_id"]) for i in items)
            if len(names) >= int(r.get("total") or 0) or not items:
                return SubjectServices(
                    frozenset(names), LEVEL_RANK.get(type_level, 0) >= LEVEL_RANK[min_level]
                )
            page += 1

    async def usable(self, subject: str) -> SubjectServices:
        return await self._accessible(subject, "READ")

    async def owned(self, user_id: str) -> SubjectServices:
        return await self._accessible(user_id, "OWNER")

    async def level(self, user_id: str, service: str) -> str:
        try:
            r = await self._call(
                "GET",
                "/open/v1/data-permissions/check",
                params={"data_code": DATA_CODE, "data_id": service, "user_id": user_id},
            )
        except NotFound:
            return "NONE"
        return str(r.get("permission") or "NONE")

    async def register(self, service: str, display_name: str, owner: str) -> None:
        try:
            await self._call(
                "POST",
                "/open/v1/data-permissions/batch",
                json={
                    "data_code": DATA_CODE,
                    "data_id": service,
                    "data_name": (display_name or service)[:100],
                    "grants": [
                        {"permission": "OWNER", "subject_type": "USER", "subject_id": owner}
                    ],
                },
            )
        except Forbidden as exc:
            # OWNER_REQUIRED：这条数据已经有授权（服务名不能复用，§3.2）
            raise Conflict(
                f"用户中心里已经登记过 MCP 服务 {service}", code="UC_REGISTERED"
            ) from exc

    async def rename(self, service: str, display_name: str) -> None:
        await self._call(
            "PATCH",
            "/open/v1/data-objects",
            json={"data_code": DATA_CODE, "data_id": service, "data_name": display_name[:100]},
        )

    async def acl(self, service: str) -> list[dict[str, Any]]:
        try:
            return list(
                await self._call(
                    "GET",
                    "/open/v1/data-permissions/acl",
                    params={"data_code": DATA_CODE, "data_id": service},
                )
            )
        except NotFound:
            return []

    async def grant(self, service: str, operator: str, grants: list[Grant]) -> None:
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": DATA_CODE,
                "data_id": service,
                "operator_id": operator,
                "grants": [
                    {
                        "permission": g.level,
                        "subject_type": g.subject_type.upper(),
                        "subject_id": g.subject_id,
                        "include_sub": g.include_sub,
                    }
                    for g in grants
                ],
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

    async def holders(self, service: str) -> list[dict[str, Any]]:
        try:
            return list(
                await self._call(
                    "GET",
                    "/open/v1/data-permissions/holders",
                    params={"data_code": DATA_CODE, "data_id": service, "min": "READ"},
                )
            )
        except NotFound:
            return []

    async def user(self, user_id: str) -> dict[str, Any] | None:
        """用户中心里的用户（人或数字员工）；不存在为 None。"""
        try:
            return dict(await self._call("GET", f"/open/v1/users/{user_id}"))
        except NotFound:
            return None

    async def user_by_account(self, account: str) -> dict[str, Any] | None:
        try:
            return dict(await self._call("GET", f"/open/v1/users/by-account/{account}"))
        except NotFound:
            return None


class UsableCache:
    """「主体 → 能用的服务」的短时缓存。控制面（替 Atlas 回答）和数据面（每个请求判权）共用：
    两道闸门的判断依据一致（§5.4）。

    ★ 用户中心不可用时：缓存没过期就继续用，过期了一律拒绝（Unavailable 往上抛）。
    """

    def __init__(self, inner: Permissions, ttl_s: float) -> None:
        self.inner = inner
        self._ttl = ttl_s
        self._rows: dict[str, tuple[float, SubjectServices]] = {}

    async def usable(self, subject: str) -> SubjectServices:
        row = self._rows.get(subject)
        if row is not None and row[0] >= time.monotonic():
            return row[1]
        found = await self.inner.usable(subject)
        if self._ttl > 0:
            if len(self._rows) > 50_000:
                self._rows.clear()
            self._rows[subject] = (time.monotonic() + self._ttl, found)
        return found

    def forget(self, subject: str | None = None) -> None:
        """本进程里刚改过授权：立即生效（别的副本仍要等缓存过期）。"""
        if subject is None:
            self._rows.clear()
        else:
            self._rows.pop(subject, None)
