"""运营审计（c4-model/opscenter/code-access.html · AuditLog）：只追加，不改不删。

★ 写接口在接口层统一记一条「发起」；跨系统的操作结果要等很久，由租户领域在操作结束时补记。
★ detail 只放参数摘要：密码、临时密码、密钥一律不进审计（调用方传进来之前就要去掉）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEntry
from .db.types import utcnow

__all__ = ["AuditFilter", "Who", "complete", "record", "search"]

#: 永远不进审计的字段名（不管嵌在哪一层）
_SECRET_KEYS = {"password", "old_password", "new_password", "temp_password", "app_secret", "secret"}


@dataclass(frozen=True)
class Who:
    """做这件事的运营人员（用户中心里系统租户的人）；后台任务自动发起时 uuid 为空。"""

    operator_uuid: UUID | None = None
    name: str | None = None

    @classmethod
    def system(cls) -> Who:
        return cls(None, "运营中心")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def record(
    session: AsyncSession,
    who: Who,
    action: str,
    *,
    module: str | None = None,
    target: str | None = None,
    tenant_uuid: UUID | None = None,
    detail: dict[str, Any] | None = None,
    result: str | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        # 显式给：调用方 flush 之前就要引用它（租户操作记下「发起」那条审计）
        audit_uuid=uuid4(),
        operator_uuid=who.operator_uuid,
        operator_name=(who.name or "")[:32] or None,
        action=action[:64],
        module=module,
        target=(target or "")[:200] or None,
        tenant_uuid=tenant_uuid,
        detail=_clean(detail) if detail else None,
        result=result,
        finished_at=utcnow() if result else None,
    )
    session.add(entry)
    return entry


async def complete(session: AsyncSession, audit_uuid: UUID, result: str) -> None:
    await session.execute(
        update(AuditEntry)
        .where(AuditEntry.audit_uuid == audit_uuid)
        .values(result=result, finished_at=utcnow())
    )


@dataclass(frozen=True)
class AuditFilter:
    action: str | None = None
    #: 模块的数据编码
    module: str | None = None
    tenant_uuid: UUID | None = None
    operator_uuid: UUID | None = None
    before: datetime | None = None
    limit: int = 50


async def search(session: AsyncSession, f: AuditFilter) -> list[AuditEntry]:
    stmt = select(AuditEntry).order_by(AuditEntry.at.desc(), AuditEntry.id.desc())
    if f.action:
        stmt = stmt.where(AuditEntry.action.like(f"{f.action}%"))
    if f.module:
        stmt = stmt.where(AuditEntry.module == f.module)
    if f.tenant_uuid:
        stmt = stmt.where(AuditEntry.tenant_uuid == f.tenant_uuid)
    if f.operator_uuid:
        stmt = stmt.where(AuditEntry.operator_uuid == f.operator_uuid)
    if f.before:
        stmt = stmt.where(AuditEntry.at < f.before)
    return list(await session.scalars(stmt.limit(max(1, min(f.limit, 200)))))
