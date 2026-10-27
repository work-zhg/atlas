"""管理动作的审计（§12）。★ 不记 Key 原文。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import GwAudit

__all__ = ["record"]


def record(session: AsyncSession, *, actor: str, action: str, target: str, **detail: Any) -> None:
    session.add(GwAudit(actor=actor, action=action, target=target, detail=detail))
