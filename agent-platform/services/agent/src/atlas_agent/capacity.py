"""容量（c4-model/agent/code-capacity.html）：租户份额和每人上限的准入。

共用设施分三层：总容量（部署的执行能力：执行器每实例的槽位、集群资源，不在这里）、租户份额
（运营中心经运营接口定，tenant_capacity）、每人上限（租户设置，应用配置库）。本组件管后两层。

    admit_run       Run 执行器认领 run 的事务里：锁住租户的份额行，数持有租约的 run
    admit_sandbox   沙箱供给登记新沙箱的事务里：同样先锁行，数在建 / 就绪的沙箱
    admit_storage   上传前：租户的用量是各人最近一次现算的用量之和（软上限）

★ 先锁行再数，同一个租户的认领串行，不会超（先数再建在并发时只是软上限）。不用 Redis 计数：
  计数会和真实状态漂移，进程崩了还要回收。
★ 用量从各自的数据里数：在跑的 run = 持有有效租约的 run（挂起、排队、租约过期等接管的都不算 ——
  否则份额满时连接管孤儿 run 都会被它自己挡住）；沙箱 = 没退役的。存储例外（按目录统计太慢）。
★ 0 / 空 = 不限。默认份额和每人上限的系统值都是 0：没配就和多租户之前一样。
★ 准入要用的配置（默认份额、每人上限）由调用方在**打开准入事务之前**读好（limits），事务里只锁行、
  数数。读配置要另借一个连接：持有事务的连接时再借，同时准入的一多，整个连接池会被「拿着一个、
  等第二个」的准入占满，一起等到超时（2026-11-26 日本服务器并发压测，24 个同时开跑就卡死 30 秒）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from atlas_tenancy import TenantScope
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Run, StorageUsageRow, TenantCapacityRow, UserSandboxRow

if TYPE_CHECKING:
    from .runtime_config import RuntimeConfig

__all__ = ["Admission", "CapacityService", "Limits", "Quota", "ResourceKind", "TenantUsage"]

#: 现算一个人的存储用量（字节）
Measure = Callable[[], Awaitable[int]]


class ResourceKind(StrEnum):
    RUN = "run"
    SANDBOX = "sandbox"
    STORAGE = "storage"


#: 占着资源的沙箱：在建、就绪；退役中的已经在释放，不算
_COUNTED_SANDBOX = ("creating", "ready")
_WHAT = {
    ResourceKind.RUN: "同时运行的任务",
    ResourceKind.SANDBOX: "沙箱和会话 Pod",
    ResourceKind.STORAGE: "工作空间存储",
}


@dataclass(frozen=True)
class Quota:
    """一个租户的份额。None = 不限。"""

    running_runs: int | None = None
    sandboxes: int | None = None
    storage_bytes: int | None = None

    def limit(self, kind: ResourceKind) -> int | None:
        return {
            ResourceKind.RUN: self.running_runs,
            ResourceKind.SANDBOX: self.sandboxes,
            ResourceKind.STORAGE: self.storage_bytes,
        }[kind]


@dataclass(frozen=True)
class Limits:
    """准入要用的配置：没单独定份额的租户用的默认份额、每人同时运行的 run。

    CapacityService.limits() 在打开准入事务之前读（见模块说明），再交给 admit_*。
    """

    defaults: Quota
    runs_per_person: int | None = None


@dataclass(frozen=True)
class Admission:
    """一次准入的结论。不行时说清哪一层满了，调用方原样给用户看。"""

    allowed: bool
    kind: ResourceKind
    layer: str | None = None  # "tenant" | "person"
    limit: int | None = None
    used: int = 0

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        who = "本租户" if self.layer == "tenant" else "你"
        return f"{who}的{_WHAT[self.kind]}已满（上限 {self.limit}，已用 {self.used}）"


@dataclass(frozen=True)
class TenantUsage:
    tenant_uuid: UUID
    quota: Quota
    is_default: bool
    running_runs: int
    sandboxes: int
    storage_bytes: int


def _none_if_zero(value: int) -> int | None:
    return value if value > 0 else None


def _judge(
    kind: ResourceKind,
    adding: int,
    tenant_used: int,
    tenant_limit: int | None,
    person_used: int,
    person_limit: int | None,
) -> Admission:
    # 每人上限高于租户份额时按份额算：租户这一层先判
    if tenant_limit is not None and tenant_used + adding > tenant_limit:
        return Admission(False, kind, "tenant", tenant_limit, tenant_used)
    if person_limit is not None and person_used + adding > person_limit:
        return Admission(False, kind, "person", person_limit, person_used)
    return Admission(True, kind)


class CapacityService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------ 份额

    async def _defaults(self) -> Quota:
        runs, sandboxes, storage = await self._config.capacity_defaults()
        return Quota(_none_if_zero(runs), _none_if_zero(sandboxes), _none_if_zero(storage))

    async def limits(self) -> Limits:
        """准入要用的配置。★ 不要在准入事务里调：先调它，再开事务（见模块说明）。"""
        defaults = await self._defaults()
        per_runs, _, _ = await self._config.per_person()
        return Limits(defaults, _none_if_zero(per_runs))

    async def _lock(self, session: AsyncSession) -> TenantCapacityRow:
        """在调用方的事务里锁住当前租户的份额行；还没有就按默认份额建（并发建的只有一个成功）。"""
        stmt = sa.select(TenantCapacityRow).with_for_update()
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row
        try:
            async with session.begin_nested():
                session.add(TenantCapacityRow(is_default=True))
        except sa.exc.IntegrityError:
            pass  # 别的请求刚建了
        return (await session.execute(stmt)).scalar_one()

    @staticmethod
    def _quota_of(row: TenantCapacityRow | None, defaults: Quota) -> Quota:
        if row is None or row.is_default:
            return defaults
        return Quota(row.running_runs, row.sandboxes, row.storage_bytes)

    # ------------------------------------------------------------------ 准入

    async def admit_run(self, session: AsyncSession, run_uuid: UUID, limits: Limits) -> Admission:
        """认领 run 的事务里调：这个租户、这个人还能不能再跑一个。limits 在开事务之前读好。"""
        quota = self._quota_of(await self._lock(session), limits.defaults)
        user = await session.scalar(sa.select(Run.created_by_uuid).where(Run.run_uuid == run_uuid))
        live = sa.and_(
            Run.status == "running",
            Run.owner.isnot(None),
            Run.lease_until > datetime.now(UTC),
            Run.run_uuid != run_uuid,
        )
        tenant_used = await session.scalar(sa.select(sa.func.count(Run.id)).where(live)) or 0
        person_used = 0
        if limits.runs_per_person and user is not None:
            person_used = (
                await session.scalar(
                    sa.select(sa.func.count(Run.id)).where(live, Run.created_by_uuid == user)
                )
                or 0
            )
        return _judge(
            ResourceKind.RUN,
            1,
            tenant_used,
            quota.running_runs,
            person_used,
            limits.runs_per_person,
        )

    async def admit_sandbox(
        self, session: AsyncSession, user_uuid: UUID, limits: Limits
    ) -> Admission:
        """登记新沙箱的事务里调（复用已有的沙箱不用问）。每人上限仍由供给的 QuotaPolicy 判。"""
        quota = self._quota_of(await self._lock(session), limits.defaults)
        if quota.sandboxes is None:
            return Admission(True, ResourceKind.SANDBOX)
        counted = UserSandboxRow.status.in_(_COUNTED_SANDBOX)
        used = await session.scalar(sa.select(sa.func.count(UserSandboxRow.id)).where(counted)) or 0
        return _judge(ResourceKind.SANDBOX, 1, used, quota.sandboxes, 0, None)

    async def admit_storage(
        self,
        session: AsyncSession,
        user_uuid: UUID,
        *,
        measure: Measure,
        adding: int,
        limits: Limits,
    ) -> Admission:
        """上传前判租户这一层（每人上限由工作空间自己判，它手里有现算的用量）。

        measure 现算这个人的用量（按目录统计，慢）：租户份额不限时不调。算出来的顺手记下，
        租户的用量是各人最近一次的用量之和 —— 软上限。
        """
        quota, _ = await self.quota(session, limits.defaults)
        if quota.storage_bytes is None:
            return Admission(True, ResourceKind.STORAGE)
        await self.record_storage(session, user_uuid, await measure())
        used = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(StorageUsageRow.bytes), 0))
        )
        return _judge(ResourceKind.STORAGE, adding, int(used or 0), quota.storage_bytes, 0, None)

    async def record_storage(self, session: AsyncSession, user_uuid: UUID, used: int) -> None:
        row = (
            await session.execute(
                sa.select(StorageUsageRow).where(StorageUsageRow.user_uuid == user_uuid)
            )
        ).scalar_one_or_none()
        if row is None:
            try:
                async with session.begin_nested():
                    session.add(StorageUsageRow(user_uuid=user_uuid, bytes=used))
            except sa.exc.IntegrityError:
                pass
            return
        row.bytes, row.measured_at = used, datetime.now(UTC)

    # ---------------------------------------------------- 份额的读写（运营接口、租户设置）

    async def quota(
        self, session: AsyncSession, defaults: Quota | None = None
    ) -> tuple[Quota, bool]:
        """当前租户的份额，以及是不是默认份额。defaults 没给就现读（先读配置，再查份额行）。"""
        if defaults is None:
            defaults = await self._defaults()
        row = (await session.execute(sa.select(TenantCapacityRow))).scalar_one_or_none()
        return self._quota_of(row, defaults), row is None or row.is_default

    async def set_quota(self, session: AsyncSession, quota: Quota | None, *, by: str) -> None:
        """改当前租户的份额；None = 回到默认份额。运营接口在这个租户的上下文里调。"""
        row = await self._lock(session)
        if quota is None:
            row.is_default = True
            row.running_runs = row.sandboxes = row.storage_bytes = None
        else:
            row.is_default = False
            row.running_runs = quota.running_runs
            row.sandboxes = quota.sandboxes
            row.storage_bytes = quota.storage_bytes
        row.updated_by = by[:64]
        row.version += 1

    async def usage(self, session: AsyncSession) -> TenantUsage:
        """当前租户的份额和用量（只有数字）。"""
        quota, is_default = await self.quota(session)
        now = datetime.now(UTC)
        runs = await session.scalar(
            sa.select(sa.func.count(Run.id)).where(
                Run.status == "running", Run.owner.isnot(None), Run.lease_until > now
            )
        )
        sandboxes = await session.scalar(
            sa.select(sa.func.count(UserSandboxRow.id)).where(
                UserSandboxRow.status.in_(_COUNTED_SANDBOX)
            )
        )
        storage = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(StorageUsageRow.bytes), 0))
        )
        return TenantUsage(
            TenantScope.require(), quota, is_default, runs or 0, sandboxes or 0, int(storage or 0)
        )


def describe(admission: Admission) -> dict[str, Any]:
    return {
        "allowed": admission.allowed,
        "kind": admission.kind.value,
        "layer": admission.layer,
        "limit": admission.limit,
        "used": admission.used,
        "message": admission.message,
    }
