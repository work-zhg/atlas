from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Approval

#: `Approval.tool_name` 的列宽（db/models.py 的 String(128)）。
_TOOL_NAME_MAX = 128


def _fit_tool_name(name: str) -> str:
    """把 tool_name 压进列宽。超长就截断，尾部留省略号。

    ★ 为什么截断而不是放宽列宽，也不是在调用方各自保证。

      这个字段对 native 来说是真的工具名（`write_todos`，十几个字符），对 acp
      来说是 CLI 给的 `toolCall.title` —— 而 Terminal 工具的 title **就是整条
      命令**，几百字符是常态。两种语义共用一列，宽度取多少都是猜。

      真正的代价在失败方向上：`RedisApprovalGate.check()` 把任何落库异常都按
      **拒绝**处理（那条红线本身是对的），于是一次 StringDataRightTruncation
      会静默地变成一次「用户拒了」—— CLI 收到 reject_once 就停手，子 run 零
      文本产出，父模型只能看到一句「没有产出文本结论」，从那里根本推不回
      列宽。2026-09-23 真机上就是这么表现的：短 title 的审批全过，长 title 的
      审批全挂，而 approval 表里干脆没有那几行。

      截断放在**仓储层**而不是 acp 侧：约束属于存储，在这里兜住能防所有写入
      方；只改 acp 的话，下一个长 tool_name 的来源会再撞一次同样的坑。

    ★ 截断不丢信息：完整的 title 和入参都在 `args`（JSONB，无长度限制），
      前端展示取的是那份。这一列只是审计用的短名。
    """
    if len(name) <= _TOOL_NAME_MAX:
        return name
    return name[: _TOOL_NAME_MAX - 1] + "…"


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        approval_id: UUID,
        run_id: UUID,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> Approval:
        """id 由 engine 生成并一同发进 approval.required 事件 ——
        前端拿到事件就能直接构造决策 URL，不必再查一次。

        ★ id 是**可复现**的（kernel 的 approval_id_for 从 run_id + tool_call_id
          派生），这是审批能挂起的前提：续跑时算出同一个 id，于是查到这条记录
          而不是重新登记一遍（那会死循环）。tool_call_id 一起存下来供审计。
        """
        row = Approval(
            id=approval_id,
            run_id=run_id,
            tool_name=_fit_tool_name(tool_name),
            args=args,
            status="pending",
            tool_call_id=tool_call_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, approval_id: UUID) -> Approval | None:
        stmt = select(Approval).where(Approval.id == approval_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_pending(self, run_id: UUID) -> list[Approval]:
        stmt = (
            select(Approval)
            .where(Approval.run_id == run_id, Approval.status == "pending")
            .order_by(Approval.created_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def decide(self, approval_id: UUID, *, decision: str, user_id: UUID) -> bool:
        """★ WHERE status='pending' 保证只有第一次决策生效。

        多标签页、重复点击、以及「用户点批准的同时后端刚好判定超时」都会
        产生并发决策；先查后写有窗口，条件写进 UPDATE 才安全。
        """
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status=decision, decided_by=user_id, decided_at=datetime.now(UTC))
        )
        return bool((await self._session.execute(stmt)).rowcount)

    async def expire(self, approval_id: UUID) -> bool:
        stmt = (
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(status="expired", decided_at=datetime.now(UTC))
        )
        return bool((await self._session.execute(stmt)).rowcount)
