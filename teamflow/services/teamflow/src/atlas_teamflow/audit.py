"""配置变更的审计：只追加，和业务写入同一个事务。流程里的事记在流程事件里，不在这里。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantOwned
from sqlalchemy import Index, String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .db.base import Base, Keys
from .db.types import JSONType, UTCDateTime, UUIDType, utcnow

__all__ = ["AuditEvent", "record"]


class AuditEvent(TenantOwned, Keys, Base):
    __tablename__ = "tf_audit_event"

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    actor: Mapped[UUID | None] = mapped_column(UUIDType)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_uuid: Mapped[UUID | None] = mapped_column(UUIDType)
    target_name: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    __table_args__ = (
        Index("ix_tf_audit_event_time", text("occurred_at DESC")),
        Index("ix_tf_audit_event_target", "target_type", "target_uuid"),
    )


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
            actor=actor,
            action=action,
            target_type=target_type,
            target_uuid=target_uuid,
            target_name=target_name,
            detail=clean or None,
        )
    )
