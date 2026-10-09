"""业务审计：只追加，与业务写入同一事务（权限变更由用户中心记录，权限设计 §13）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEvent

__all__ = ["record"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def record(
    session: AsyncSession,
    actor: UUID | None,
    action: str,
    target_type: str,
    target_uuid: UUID | None = None,
    target_name: str | None = None,
    **detail: Any,
) -> None:
    clean = {k: _jsonable(v) for k, v in detail.items() if v is not None}
    session.add(
        AuditEvent(
            actor_uuid=actor,
            action=action,
            target_type=target_type,
            target_uuid=target_uuid,
            target_name=target_name,
            detail=clean or None,
        )
    )
