"""管理动作的审计（§10）。★ 不记 Key 原文。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import McpAudit

__all__ = ["record"]


def record(session: AsyncSession, *, actor: str, action: str, target: str, **detail: Any) -> None:
    session.add(McpAudit(actor=actor, action=action, target=target, detail=detail))
