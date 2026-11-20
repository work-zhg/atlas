"""一个租户在本系统里的状态（聚合根 TenantState）、它的仓储，和给后台工作用的 TenantGate。

    ACTIVE → SUSPENDED → PURGING → PURGED；SUSPENDED 可以回到 ACTIVE，PURGING 以后不能恢复

★ 为什么本地记一份：停用以后，用户中心只挡得住新的用户请求；run 续跑、入库、记忆补记这些后台
  工作没有用户请求，挡不住。本地记一份，停用通知一到立即生效，也不用每次去问用户中心。
★ 没有行 = ACTIVE：开通租户时各系统不用做任何事。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from .db import state_table as S

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["Rejected", "TenantGate", "TenantState", "TenantStateRepository", "TenantStatus"]


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PURGING = "purging"
    PURGED = "purged"


class Rejected(Exception):
    """前置条件不满足：重做也一样，要人看（运营中心按「被拒绝」处理，不反复重试）。"""


class StateConflict(Exception):
    """别人先改了这个租户的状态：重读再做。"""


@dataclass
class TenantState:
    tenant_uuid: UUID
    status: TenantStatus = TenantStatus.ACTIVE
    operation_uuid: UUID | None = None
    version: int = 0
    changed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _to(self, status: TenantStatus, operation: UUID | None) -> None:
        self.status, self.operation_uuid, self.changed_at = status, operation, datetime.now(UTC)

    def suspend(self, operation: UUID | None) -> bool:
        """→ 状态变了没有。已经在删的租户比停用更彻底，什么都不做。"""
        if self.status is TenantStatus.ACTIVE:
            self._to(TenantStatus.SUSPENDED, operation)
            return True
        return False

    def resume(self, operation: UUID | None) -> bool:
        if self.status in (TenantStatus.PURGING, TenantStatus.PURGED):
            raise Rejected("这个租户的数据已经开始删除，不能恢复")
        if self.status is TenantStatus.SUSPENDED:
            self._to(TenantStatus.ACTIVE, operation)
            return True
        return False

    def start_purge(self, operation: UUID | None) -> bool:
        if self.status is TenantStatus.ACTIVE:
            raise Rejected("只能删除已停用的租户的数据：先停用")
        if self.status is TenantStatus.SUSPENDED:
            self._to(TenantStatus.PURGING, operation)
            return True
        return False

    def finish_purge(self) -> None:
        if self.status is TenantStatus.PURGING:
            self._to(TenantStatus.PURGED, self.operation_uuid)


def _state(row: Any) -> TenantState:
    return TenantState(
        tenant_uuid=row.tenant_uuid,
        status=TenantStatus(row.status),
        operation_uuid=row.operation_uuid,
        version=row.version,
        changed_at=row.changed_at,
    )


class TenantStateRepository:
    async def get(self, session: AsyncSession, tenant_uuid: UUID) -> TenantState:
        row = (
            await session.execute(sa.select(S).where(S.c.tenant_uuid == tenant_uuid))
        ).one_or_none()
        return _state(row) if row is not None else TenantState(tenant_uuid)

    async def all(self, session: AsyncSession) -> list[TenantState]:
        rows = (await session.execute(sa.select(S).order_by(S.c.changed_at.desc()))).all()
        return [_state(r) for r in rows]

    async def save(self, session: AsyncSession, state: TenantState) -> None:
        """按版本号 CAS；没有行时插入。别人先改了抛 StateConflict。"""
        values = {
            "status": state.status.value,
            "operation_uuid": state.operation_uuid,
            "changed_at": state.changed_at,
            "version": state.version + 1,
        }
        if state.version == 0:
            try:
                async with session.begin_nested():
                    await session.execute(
                        sa.insert(S).values(tenant_uuid=state.tenant_uuid, **values)
                    )
            except sa.exc.IntegrityError as exc:
                raise StateConflict(str(state.tenant_uuid)) from exc
        else:
            result = await session.execute(
                sa.update(S)
                .where(S.c.tenant_uuid == state.tenant_uuid, S.c.version == state.version)
                .values(**values)
            )
            if getattr(result, "rowcount", 0) != 1:
                raise StateConflict(str(state.tenant_uuid))
        state.version += 1


class TenantGate:
    """后台工作开始前问一句：这个租户还能不能用。查本地状态，进程内缓存几秒。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, ttl: float = 5.0) -> None:
        self._sessions = sessions
        self._ttl = ttl
        self._cache: dict[UUID, tuple[float, bool]] = {}
        self._repo = TenantStateRepository()

    async def is_active(self, tenant_uuid: UUID) -> bool:
        hit = self._cache.get(tenant_uuid)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        async with self._sessions() as session:
            state = await self._repo.get(session, tenant_uuid)
        active = state.status is TenantStatus.ACTIVE
        if len(self._cache) > 10_000:  # 防止无界增长
            self._cache.clear()
        self._cache[tenant_uuid] = (time.monotonic() + self._ttl, active)
        return active

    async def require_active(self, tenant_uuid: UUID) -> None:
        if not await self.is_active(tenant_uuid):
            raise Rejected("这个租户已停用")

    def invalidate(self, tenant_uuid: UUID) -> None:
        """本进程改了状态就清掉；别的实例等缓存过期（最多 ttl 秒）。"""
        self._cache.pop(tenant_uuid, None)
