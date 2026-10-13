"""写操作审计。调用方在**同一个 session** 里调用，随业务写入一起提交。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import ConfigAudit

__all__ = ["record"]


def record(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_kind: str,
    target_id: str,
    **detail: Any,
) -> None:
    session.add(
        ConfigAudit(
            actor=actor,
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )
