"""AccessControl：这个请求是谁、在哪个租户、能不能做这件事。

    认人        网关写的 X-Uid、X-Tenant-Id；没有就 401。授权快照（操作码、是否系统租户、
                「全部团队」                级别、「流程市场」级别）按（人，租户）缓存 30 秒。
    判动作      操作码（系统角色）∧ 团队有效级别（团队数据权限）。新建团队只判操作码。
    流程市场    系统租户 ∧ 「流程市场」编码级读写 = 维护；其余能登录的人只看。

★ 系统角色（用户中心里给人授的「团队管理员」「团队成员」）和模板角色（产品、开发……，项目里
  分配给人）是两回事：后者是某条流程里的事实，由流程运行判，不在这里。
★ 不知道租户状态：租户停用、到期后用户中心在授权上返回无权限。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from ..errors import Forbidden, NotFound, TFError, Unauthorized
from ..usercenter import MARKET_CODE, TEAM_CODE, UserCenter

__all__ = ["LEVEL_RANK", "AccessControl", "Action", "MarketLevel", "PeopleDirectory", "Principal"]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


def _max(*levels: str) -> str:
    return max(levels, key=lambda lv: LEVEL_RANK.get(lv, 0))


class Action(Enum):
    """入口会问的动作 → (操作码, 最低团队级别)。流程里的执行、评审在这里只判读写。"""

    TEAM_CREATE = ("team:create", None)
    TEAM_CONFIG = ("team:config", "OWNER")
    FLOW_CONFIG = ("flow:config", "OWNER")
    PROCESS_RUN = ("process:run", "WRITE")
    PROCESS_VIEW = ("process:view", "READ")

    @property
    def operation(self) -> str:
        return self.value[0]

    @property
    def min_level(self) -> str | None:
        return self.value[1]


class MarketLevel(Enum):
    MAINTAIN = "maintain"
    VIEW = "view"


@dataclass(frozen=True)
class Principal:
    """一次请求里的调用人。领域方法只收其中的 uid。"""

    uid: UUID
    tenant: UUID
    name: str
    operations: frozenset[str]
    menus: tuple[Any, ...]
    system_tenant: bool
    #: 「全部团队」编码级授权的级别
    team_all: str
    #: 「流程市场」编码级授权的级别（只在系统租户里取）
    market: str

    def has(self, op: str) -> bool:
        return op in self.operations


class _Ttl:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._rows: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        row = self._rows.get(key)
        if row is None or time.monotonic() - row[0] > self.ttl:
            return None
        return row[1]

    def put(self, key: Any, value: Any) -> None:
        if len(self._rows) > 20_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic(), value)

    def drop(self, pred: Any) -> None:
        for key in [k for k in self._rows if pred(k)]:
            self._rows.pop(key, None)

    def clear(self) -> None:
        self._rows.clear()


class AccessControl:
    def __init__(self, uc: UserCenter, *, ttl_seconds: float = 30.0) -> None:
        self.uc = uc
        self._snapshots = _Ttl(ttl_seconds)
        self._levels = _Ttl(ttl_seconds)
        self._system = _Ttl(300.0)

    # ───────────────────────────── 认人

    async def principal(self, headers: Mapping[str, str]) -> Principal:
        try:
            uid = UUID((headers.get("x-uid") or "").strip())
        except ValueError as exc:
            raise Unauthorized("NOT_LOGGED_IN", "请先登录") from exc
        try:
            tenant = UUID((headers.get("x-tenant-id") or "").strip())
        except ValueError as exc:
            raise Unauthorized("NO_TENANT", "缺少租户（X-Tenant-Id）") from exc
        p = self._snapshots.get((uid, tenant))
        if p is None:
            p = await self._snapshot(uid, tenant)
            self._snapshots.put((uid, tenant), p)
        return p

    async def _snapshot(self, uid: UUID, tenant: UUID) -> Principal:
        got = await self.uc.authz(uid)
        user = got.get("user") or {}
        if user.get("status") == "disabled":
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用")
        if user.get("tenant_id") and UUID(str(user["tenant_id"])) != tenant:
            raise Unauthorized("TENANT_MISMATCH", "请求头里的租户和用户所属的不一致")
        if not got.get("can_access"):
            raise Forbidden("NO_ACCESS", "你没有访问 AI TeamFlow 的权限，请联系管理员")
        system = await self.is_system_tenant(tenant)
        team_all, market = await asyncio.gather(
            self.uc.type_level(TEAM_CODE, uid),
            self.uc.type_level(MARKET_CODE, uid) if system else _none(),
        )
        return Principal(
            uid=uid,
            tenant=tenant,
            name=str(user.get("name") or ""),
            operations=frozenset(got.get("permissions") or []),
            menus=tuple(got.get("menus") or []),
            system_tenant=system,
            team_all=team_all,
            market=market,
        )

    async def is_system_tenant(self, tenant: UUID) -> bool:
        ids = self._system.get("ids")
        if ids is None:
            ids = {UUID(t["id"]) for t in await self.uc.tenants() if t.get("is_system")}
            self._system.put("ids", ids)
        return tenant in ids

    # ───────────────────────────── 团队级别

    async def team_level(self, p: Principal, team_uuid: UUID) -> str:
        """有效级别 = 单条授权和「全部团队」里高的（用户中心的 check 已经合在一起）。"""
        if LEVEL_RANK[p.team_all] >= LEVEL_RANK["OWNER"]:
            return p.team_all
        key = (p.uid, p.tenant, team_uuid)
        level = self._levels.get(key)
        if level is None:
            level = await self.uc.data_level(TEAM_CODE, team_uuid, p.uid)
            self._levels.put(key, level)
        return _max(level, p.team_all)

    def drop_team(self, team_uuid: UUID) -> None:
        """团队成员、级别变了：作废这个团队的级别缓存（各副本经协调层广播调）。"""
        self._levels.drop(lambda k: k[2] == team_uuid)

    async def visible_teams(self, p: Principal) -> set[UUID] | None:
        """能看哪些团队：有「全部团队」级别的返回 None（全部），否则是有单条授权的。"""
        if LEVEL_RANK[p.team_all] >= LEVEL_RANK["READ"]:
            return None
        rows = await self.uc.accessible(TEAM_CODE, p.uid, "READ")
        return {UUID(k) for k in rows}

    # ───────────────────────────── 判动作

    async def require(self, p: Principal, action: Action, team_uuid: UUID | None = None) -> None:
        if not p.has(action.operation):
            raise Forbidden(
                "PERMISSION_DENIED",
                f"缺少操作权限：{action.operation}",
                missing=[action.operation],
            )
        if action.min_level is None or team_uuid is None:
            return
        level = await self.team_level(p, team_uuid)
        if LEVEL_RANK[level] >= LEVEL_RANK[action.min_level]:
            return
        if LEVEL_RANK[level] == 0:
            # 看不到这个团队：和不存在一样
            raise NotFound("TEAM_NOT_FOUND", "团队不存在")
        need = {"OWNER": "团队管理员", "WRITE": "团队的读写成员", "READ": "团队成员"}
        raise Forbidden("TEAM_LEVEL_REQUIRED", f"只有{need[action.min_level]}可以做这件事")

    def market_level(self, p: Principal) -> MarketLevel:
        if p.system_tenant and LEVEL_RANK[p.market] >= LEVEL_RANK["WRITE"]:
            return MarketLevel.MAINTAIN
        return MarketLevel.VIEW

    def require_market(self, p: Principal) -> None:
        if self.market_level(p) is not MarketLevel.MAINTAIN:
            raise Forbidden("MARKET_MAINTAIN_REQUIRED", "只有运营管理员可以维护流程市场")


async def _none() -> str:
    return "NONE"


class PeopleDirectory:
    """人的名字：流程、评审、成员列表只存 uid，显示时按 uid 向用户中心取，进程内缓存 10 分钟。"""

    def __init__(self, uc: UserCenter, *, ttl_seconds: float = 600.0) -> None:
        self.uc = uc
        self._cache = _Ttl(ttl_seconds)

    def remember(self, uid: UUID, name: str, account: str | None = None) -> None:
        self._cache.put(uid, {"id": str(uid), "name": name, "account": account})

    async def names(self, uids: list[UUID]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        missing: list[UUID] = []
        for uid in dict.fromkeys(uids):
            hit = self._cache.get(uid)
            if hit is None:
                missing.append(uid)
            else:
                out[str(uid)] = hit
        sem = asyncio.Semaphore(8)

        async def one(uid: UUID) -> None:
            async with sem:
                try:
                    u = await self.uc.user(uid)
                except TFError:
                    return
                self.remember(uid, str(u.get("name") or ""), u.get("account"))
                out[str(uid)] = self._cache.get(uid)

        await asyncio.gather(*(one(u) for u in missing))
        return out
