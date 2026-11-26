"""权限与审计（c4-model/opscenter/code-access.html、design-opscenter.html）。

运营中心不再有自己的账号和登录。运营人员是用户中心里「系统租户」的人：统一登录，请求经 API 网关验过
访问 JWT、写上 X-Uid、X-Tenant-Id 才进来。

    能不能进   用户中心的授权快照（运营中心这个应用标了「仅系统租户」：普通租户的人一律不可访问）
    能做什么   按模块：租户模块，加上每个登记的系统一个模块；每个模块在用户中心里是一个数据编码，
               系统租户的管理员给人授它的「全部」级别 —— 只读能看，读写能改，
               Owner 还能做不可恢复的操作
    跨模块的   只有查运营审计，要操作码 ops:audit（内置角色「运营审计员」）

★ 用户中心的结果按 (用户, 租户) 缓存 authz_cache_s 秒：收回授权、停用账号最长这么久生效。
★ 运营中心自己的应用目录（应用 opscenter、每个模块的数据编码、操作码、内置角色）随代码同步进用户中心
  （CatalogSync），授权仍由人在用户中心里做。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any
from uuid import UUID

import httpx
from atlas_jobs import Job, JobContext, NoParams

from .audit import Who
from .errors import Forbidden, Unauthorized, Upstream
from .gateway import CallFailed, Capability, OpsClient, SystemEntry, SystemRegistry, stamp_of

__all__ = [
    "AUDIT_OP",
    "TENANT_MODULE",
    "CatalogSync",
    "Level",
    "ModuleCatalog",
    "OpsModule",
    "Principal",
    "PrincipalResolver",
    "UserCenterOpen",
    "required_level",
]

logger = logging.getLogger(__name__)

#: 运营中心在用户中心里的应用编码（内网调开放接口时 X-Caller-App 写它）
APP_CODE = "opscenter"
#: 租户模块的数据编码（开通、停用、恢复、关闭租户，给租户开通应用）
TENANT_MODULE = "OpsTenant"
#: 跨模块的功能：查运营审计
AUDIT_OP = "ops:audit"


class Level(IntEnum):
    """模块级别，和用户中心数据权限的三级一一对应。"""

    NONE = 0
    #: 只读：看
    READ = 1
    #: 读写：日常变更
    WRITE = 2
    #: 再加不可恢复的操作（关闭租户、补发临时密码、轮换应用凭据、删目录里的东西）
    OWNER = 3

    @classmethod
    def parse(cls, value: Any) -> Level:
        return cls.__members__.get(str(value or "NONE").upper(), cls.NONE)


@dataclass(frozen=True)
class OpsModule:
    """运营中心的一个模块，也是用户中心里的一个数据编码。"""

    code: str
    name: str
    #: 对应的系统；租户模块为空
    system: str | None = None


class ModuleCatalog:
    """有哪些模块：租户模块，加上系统登记里的每个系统一个（数据编码写在登记里）。"""

    def __init__(self, registry: SystemRegistry) -> None:
        self._tenant = OpsModule(TENANT_MODULE, "租户运营")
        self._systems = {
            e.code: OpsModule(e.module_code, f"{e.name}运营", e.code) for e in registry.all()
        }
        codes = [self._tenant.code, *(m.code for m in self._systems.values())]
        if len(set(codes)) != len(codes):
            raise ValueError(f"运营模块的数据编码有重复：{codes}")

    def all(self) -> list[OpsModule]:
        return [self._tenant, *self._systems.values()]

    def tenant(self) -> OpsModule:
        return self._tenant

    def of_system(self, code: str) -> OpsModule:
        return self._systems[code]

    def for_proxy(self, entry: SystemEntry, path: str) -> OpsModule:
        """系统代理的请求属于哪个模块：一般是那个系统的；用户中心的 /tenants/… 是租户模块
        （改租户的名额、到期，给租户开通应用 —— 管租户的人做的事）。"""
        if entry.has(Capability.USER_CENTER) and path.startswith("/tenants"):
            return self._tenant
        return self.of_system(entry.code)


def required_level(entry: SystemEntry, method: str, path: str) -> Level:
    """系统代理的一个请求要什么级别：看要只读，改要读写；不可恢复的要 Owner。"""
    from .gateway import owner_only

    if method.upper() == "GET":
        return Level.READ
    return Level.OWNER if owner_only(entry, method, path) else Level.WRITE


@dataclass(frozen=True)
class Principal:
    """一个请求的运营人员：用户中心里系统租户的一个人。随调用写进 OperatorStamp 带给业务系统。"""

    user_uuid: UUID
    tenant_uuid: UUID
    name: str
    levels: dict[str, Level] = field(default_factory=dict)
    operations: frozenset[str] = frozenset()

    @property
    def who(self) -> Who:
        return Who(self.user_uuid, self.name)

    def level(self, module: OpsModule) -> Level:
        return self.levels.get(module.code, Level.NONE)

    def require(self, module: OpsModule, need: Level) -> None:
        """唯一的判断规则：这个人在这个模块的级别 ≥ 这个动作要的级别。"""
        if self.level(module) < need:
            raise Forbidden(
                "MODULE_FORBIDDEN",
                f"你在「{module.name}」的权限不够（要{_LEVEL_TEXT[need]}）",
                module=module.code,
                need=need.name,
            )

    def require_op(self, code: str) -> None:
        if code not in self.operations:
            raise Forbidden("PERMISSION_DENIED", f"缺少操作权限：{code}", missing=[code])


_LEVEL_TEXT = {Level.READ: "只读", Level.WRITE: "读写", Level.OWNER: "Owner"}


class UserCenterOpen:
    """用户中心开放接口的客户端（和 Atlas、知识库同一套：内网请求头，调用方是运营中心这个应用）。"""

    def __init__(
        self, base_url: str, *, timeout: float, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, uid: UUID, tid: UUID, **params: Any) -> dict[str, Any]:
        headers = {"X-Caller-App": APP_CODE, "X-Tenant-Id": str(tid), "X-Uid": str(uid)}
        try:
            r = await self._http.get(f"/open/v1{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise Upstream("UC_UNAVAILABLE", "用户中心暂时连不上") from exc
        if r.status_code >= 500:
            raise Upstream("UC_UNAVAILABLE", "用户中心暂时不可用")
        if r.status_code >= 400:
            # 没开通运营中心、租户不对、人不存在：一律当作「不是运营人员」
            raise Forbidden("NOT_OPERATOR", "你还没有开通运营中心（只有系统租户的人能用）")
        return r.json()  # type: ignore[no-any-return]

    async def authz(self, uid: UUID, tid: UUID) -> dict[str, Any]:
        return await self._get(f"/users/{uid}/authz", uid, tid)

    async def type_level(self, uid: UUID, tid: UUID, data_code: str) -> Level:
        try:
            data = await self._get(
                "/data-permissions/type", uid, tid, data_code=data_code, user_id=uid
            )
        except Forbidden:
            # 这个编码还没同步进用户中心（新登记的系统）：当作没有授权
            return Level.NONE
        return Level.parse(data.get("permission"))


class PrincipalResolver:
    """每个请求解析出运营人员：网关写的身份头 → 用户中心的授权快照和各模块的级别（缓存）。"""

    def __init__(self, uc: UserCenterOpen, catalog: ModuleCatalog, cache_s: float) -> None:
        self.uc = uc
        self.catalog = catalog
        self.cache_s = cache_s
        self._cache: dict[tuple[UUID, UUID], tuple[float, Principal]] = {}

    async def resolve(self, headers: Any) -> Principal:
        try:
            uid = UUID(headers.get("x-uid") or "")
            tid = UUID(headers.get("x-tenant-id") or "")
        except ValueError as exc:
            # 没有身份头 = 没经网关登录：控制台跳统一登录页
            raise Unauthorized("NOT_LOGGED_IN", "请先登录") from exc
        hit = self._cache.get((uid, tid))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        snap = await self.uc.authz(uid, tid)
        if not snap.get("can_access") or snap.get("user", {}).get("tenant_id") != str(tid):
            raise Forbidden("NOT_OPERATOR", "你还没有开通运营中心（只有系统租户的人能用）")
        modules = self.catalog.all()
        levels = await asyncio.gather(*(self.uc.type_level(uid, tid, m.code) for m in modules))
        p = Principal(
            uid,
            tid,
            snap["user"].get("name") or "",
            {m.code: lv for m, lv in zip(modules, levels, strict=True)},
            frozenset(snap.get("permissions") or ()),
        )
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[(uid, tid)] = (time.monotonic() + self.cache_s, p)
        return p


class CatalogSync(Job):
    """把运营中心自己的应用目录同步进用户中心，再给每个系统租户开通（只在还没开通时：开成全员可访问；
    之后范围由系统租户的管理员自己调）。按编码幂等，定时重跑：用户中心晚起来、新建了系统租户、
    新登记了系统，都会补上。只写目录，不写授权。"""

    type = "opscenter.catalog_sync"
    description = "把运营中心的应用目录（模块的数据编码等）同步进用户中心，给系统租户开通"

    def __init__(self, client: OpsClient, catalog: ModuleCatalog, home_url: str) -> None:
        self.client = client
        self.catalog = catalog
        self.home_url = home_url

    def spec(self) -> dict[str, Any]:
        return {
            "name": "运营中心",
            "description": "管平台本身：租户的开通与关闭，各系统的运营。只能开给系统租户",
            "icon": "🛠",
            "home_url": self.home_url,
            "system_only": True,
            "operations": [{"module": "运营审计", "code": AUDIT_OP, "name": "查看运营审计"}],
            "data_types": [
                {
                    "code": m.code,
                    "name": m.name,
                    "description": "授「全部」级别：只读能看，读写能改，Owner 还能做不可恢复的操作",
                }
                for m in self.catalog.all()
            ],
            "roles": [
                {
                    "code": "OPS_AUDITOR",
                    "name": "运营审计员",
                    "description": "看全部模块的运营审计，什么都改不了",
                    "operations": [AUDIT_OP],
                }
            ],
        }

    async def run(self) -> None:
        uc = self.client.registry.user_center.code
        stamp = stamp_of(None, Who.system().name)
        app = await self.client.call(
            uc, "PUT", f"/catalog/apps/{APP_CODE}", self.spec(), stamp=stamp
        )
        for t in await self.client.call(uc, "GET", "/tenants", stamp=stamp) or []:
            if not t.get("is_system"):
                continue
            apps = await self.client.call(uc, "GET", f"/tenants/{t['id']}/apps", stamp=stamp) or []
            if any(a["app"]["id"] == app["id"] and a["enabled"] for a in apps):
                continue
            await self.client.call(
                uc,
                "PUT",
                f"/tenants/{t['id']}/apps/{app['id']}",
                {"enabled": True, "scope_all": True},
                stamp=stamp,
            )
            logger.info("已给系统租户 %s 开通运营中心", t["code"])

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        try:
            await self.run()
        except CallFailed as exc:  # 用户中心还没起来、拒绝了：下一轮再来
            logger.warning("同步运营中心的应用目录失败：%s", exc.message)
