"""租户生命周期在本系统这一侧的流程：挂起、恢复、删除。

    挂起   本地状态改成 SUSPENDED（TenantGate 立即挡住新的后台工作）→ 宿主的 on_suspend
    恢复   宿主的 on_resume → 改回 ACTIVE
    删除   只对已挂起的；PURGING → 宿主删外部资源（分批）→ 外部删完再分批删数据库 → PURGED

★ 宿主的钩子在这个租户的上下文里跑（自动过滤照常生效）；删数据库在 OPS 系统范围里。
★ 钩子都要幂等：上一次可能做了一半就超时，运营中心会用同一个幂等键重做。
★ 运营请求记录（app_ops_request）：挂起、恢复做完就记，重复请求直接返回上次的结果；删除是分批的，
  运营中心用同一个幂等键一批批调，所以只在全部删完时才记 —— 记早了，后面的批次就被当成重复请求。
★ 先删外部、后删数据库：数据库里的行是找到文件、向量集合、Pod 的线索。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from atlas_tenancy import PurgeReport, SystemReason, TenantDataPurge, TenantScope

from .db import request_table as Q
from .state import Rejected, TenantGate, TenantState, TenantStateRepository, TenantStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from .stamp import OperatorStamp

__all__ = ["OpsRequestLedger", "Rejected", "TenantLifecycle", "TenantLifecycleService"]

logger = logging.getLogger(__name__)


class TenantLifecycle(Protocol):
    """宿主实现：自己的后台工作怎么停、怎么接着做；数据库之外还有哪些东西要删。都要幂等。"""

    async def on_suspend(self, tenant_uuid: UUID) -> None: ...

    async def on_resume(self, tenant_uuid: UUID) -> None: ...

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        """删数据库之外的东西，每次最多 limit 个。

        如对象存储、向量、Pod、用户中心里登记的数据权限。
        """
        ...


class NoExternal:
    """没有数据库之外的东西要删、也没有后台工作要停的宿主（如技能、记忆服务）用这个。"""

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        return None

    async def on_resume(self, tenant_uuid: UUID) -> None:
        return None

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        return PurgeReport()


class OpsRequestLedger:
    async def seen(self, session: AsyncSession, key: UUID | None) -> dict[str, Any] | None:
        if key is None:
            return None
        row = (
            await session.execute(sa.select(Q.c.result).where(Q.c.idempotency_key == key))
        ).one_or_none()
        return None if row is None else (row.result or {})

    async def record(
        self,
        session: AsyncSession,
        stamp: OperatorStamp,
        action: str,
        tenant_uuid: UUID | None,
        result: dict[str, Any],
    ) -> None:
        values = {
            "idempotency_key": stamp.idempotency_key,
            "action": action,
            "tenant_uuid": tenant_uuid,
            "operator_uuid": stamp.operator_uuid,
            "operator_name": stamp.operator_name[:64] or None,
            "operation_uuid": stamp.operation_uuid,
            "result": result,
            "created_at": datetime.now(UTC),
        }
        try:
            async with session.begin_nested():
                await session.execute(sa.insert(Q).values(**values))
        except sa.exc.IntegrityError:
            # 并发的重复请求先记了：结果一样，不再记一遍
            logger.info("运营请求 %s 已经记过", stamp.idempotency_key)


def _finish_purge(state: TenantState) -> bool:
    state.finish_purge()
    return True


def _report_out(report: PurgeReport) -> dict[str, Any]:
    return {"deleted": dict(report.deleted), "remaining": report.remaining}


class TenantLifecycleService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        hooks: TenantLifecycle,
        gate: TenantGate,
        purge: TenantDataPurge | None = None,
        *,
        purge_limit: int = 5000,
    ) -> None:
        self._sessions = sessions
        self._hooks = hooks
        self._gate = gate
        self._purge = purge
        self._limit = purge_limit
        self._states = TenantStateRepository()
        self._ledger = OpsRequestLedger()

    async def _change(self, tenant_uuid: UUID, change: Any) -> TenantState:
        """读状态 → 改 → CAS 保存。别人先改了就重读重做（最多几次）。"""
        from .state import StateConflict

        for _ in range(5):
            async with self._sessions() as session:
                state = await self._states.get(session, tenant_uuid)
                if change(state):
                    try:
                        await self._states.save(session, state)
                    except StateConflict:
                        await session.rollback()
                        continue
                    await session.commit()
                self._gate.invalidate(tenant_uuid)
                return state
        raise Rejected("这个租户的状态正被同时修改，稍后重试")

    async def _seen(self, stamp: OperatorStamp) -> dict[str, Any] | None:
        async with self._sessions() as session:
            return await self._ledger.seen(session, stamp.idempotency_key)

    async def _record(
        self, stamp: OperatorStamp, action: str, tenant_uuid: UUID, result: dict[str, Any]
    ) -> None:
        async with self._sessions() as session:
            await self._ledger.record(session, stamp, action, tenant_uuid, result)
            await session.commit()

    async def suspend(self, tenant_uuid: UUID, stamp: OperatorStamp) -> dict[str, Any]:
        if (done := await self._seen(stamp)) is not None:
            return done
        state = await self._change(tenant_uuid, lambda s: s.suspend(stamp.operation_uuid))
        with TenantScope.enter(tenant_uuid):
            await self._hooks.on_suspend(tenant_uuid)
        result = {"status": state.status.value}
        await self._record(stamp, "tenant.suspend", tenant_uuid, result)
        return result

    async def resume(self, tenant_uuid: UUID, stamp: OperatorStamp) -> dict[str, Any]:
        if (done := await self._seen(stamp)) is not None:
            return done
        async with self._sessions() as session:
            current = await self._states.get(session, tenant_uuid)
        current.resume(stamp.operation_uuid)  # 已经在删的，这里就拒绝，不去调钩子
        with TenantScope.enter(tenant_uuid):
            await self._hooks.on_resume(tenant_uuid)
        state = await self._change(tenant_uuid, lambda s: s.resume(stamp.operation_uuid))
        result = {"status": state.status.value}
        await self._record(stamp, "tenant.resume", tenant_uuid, result)
        return result

    async def purge(self, tenant_uuid: UUID, stamp: OperatorStamp) -> dict[str, Any]:
        if (done := await self._seen(stamp)) is not None:
            return done
        state = await self._change(tenant_uuid, lambda s: s.start_purge(stamp.operation_uuid))
        if state.status is TenantStatus.PURGED:
            result = {"deleted": {}, "remaining": False}
            await self._record(stamp, "tenant.purge", tenant_uuid, result)
            return result
        with TenantScope.enter(tenant_uuid):
            report = await self._hooks.purge_external(tenant_uuid, self._limit)
        if not report.remaining and self._purge is not None:
            async with self._sessions() as session:
                with TenantScope.system(SystemReason.OPS):
                    report.merge(await self._purge.batch(session, tenant_uuid, self._limit))
                await session.commit()
        result = _report_out(report)
        if not report.remaining:
            await self._change(tenant_uuid, _finish_purge)
            await self._record(stamp, "tenant.purge", tenant_uuid, result)
        return result

    async def states(self) -> list[TenantState]:
        async with self._sessions() as session:
            return await self._states.all(session)
