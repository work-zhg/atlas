"""存量迁移（内网身份传递 §14）：业务应用（TeamFlow）建的会话改归真实的人。

去服务账号之前，TeamFlow 的节点会话归 TeamFlow 服务账号。迁移 0020 之后它们仍能经 TeamFlow
访问（同一应用来访不要求 uid = 所有者），但不在任何人的会话列表里。TeamFlow 导出
「会话 id → 流程发起人」映射（python -m atlas_teamflow export-thread-owners），这里改写：

  · 会话与它的子会话：created_by = 映射里的人
  · 这些会话上没有发起人的 run（监督器自动投递的）：created_by / auth_uid = 同一个人

可重复执行：已经改写过的再改一遍结果相同。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update

from ..db.models import Run, Thread
from ..db.session import get_sessionmaker

__all__ = ["reassign_threads"]


async def reassign_threads(mapping: dict[str, str]) -> dict[str, Any]:
    out = {"threads": 0, "runs": 0, "missing": 0}
    async with get_sessionmaker()() as session:
        for raw_thread, raw_user in mapping.items():
            thread_id, user_id = UUID(str(raw_thread)), UUID(str(raw_user))
            ids = [
                thread_id,
                *await session.scalars(
                    select(Thread.id).where(Thread.parent_thread_id == thread_id)
                ),
            ]
            changed = await session.execute(
                update(Thread).where(Thread.id.in_(ids)).values(created_by=user_id)
            )
            if not changed.rowcount:  # type: ignore[attr-defined]
                out["missing"] += 1
                continue
            out["threads"] += changed.rowcount  # type: ignore[attr-defined]
            unowned = or_(Run.created_by.is_(None), Run.auth_uid.is_(None))
            runs = await session.execute(
                update(Run)
                .where(Run.thread_id.in_(ids), unowned)
                .values(created_by=user_id, auth_uid=user_id)
            )
            out["runs"] += runs.rowcount  # type: ignore[attr-defined]
        await session.commit()
    return out
