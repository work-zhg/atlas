"""续跑要的读查询（Run 执行器 · 续跑、接管与取消，code-executor-continuation.html）。

真正挂起的 run（有等待项的）、某个 run 还没结束的直接子 run。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Run
from .run import (
    _SUSPENDED,
    _UNFINISHED,
)


class ContinuationQueries:
    """挂起的 run 和子 run 的状态。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suspended_runs(self) -> list[UUID]:
        """**真正挂起**的 run（含等审批的）。周期扫描用来补唤醒。

        ★ 为什么需要补：正常路径是「等的东西到了就主动唤醒」—— 子 run 收尾时、
          HTTP 决策提交时。但那一刻进程可能正好在重启，唤醒信号就这么没了，
          run 会永远挂着。这个扫描是它唯一的出路。

        ★ 没有等待项的挂起（旧格式的遗留数据）不续跑。CLI 在线等权限不是挂起：它是
          running + awaiting_user，进程还在跑 —— 续跑它等于把同一个 run 执行两遍，第二条
          上游连接会把第一个按 CLOSE_SUPERSEDED(4409) 关掉，整轮崩掉。
        """
        stmt = select(Run.run_uuid).where(Run.status.in_(_SUSPENDED), Run.waiting_on.isnot(None))
        return list((await self._session.execute(stmt)).scalars())

    async def has_unfinished_children(self, run_id: UUID) -> bool:
        """这个 run 还有没跑完的子 run 吗 —— 续跑的 join barrier。

        ★ 判据直接查 run 表，不需要单独的委派账本：`parent_run_id` 本来就
          记着这层关系（迁移 0007），`ix_run_parent` 本来就在。已经终态的
          子 run（包括前几段里完成的那些）天然不计入。
        """
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(Run.parent_run_uuid == run_id, Run.status.in_(_UNFINISHED))
        )
        return int((await self._session.execute(stmt)).scalar_one()) > 0

    async def unfinished_children_of(self, run_id: UUID) -> list[UUID]:
        """还没结束的直接子 run —— 父被取消时要级联取消它们（RunService 逐层往下）。

        ★ 不级联的后果很贵：父 run 挂起时没有任何进程盯着子 run，它会一直跑下去，
          而 acp 的子 run 整段时间都占着一个 Pod。
        """
        stmt = select(Run.run_uuid).where(
            Run.parent_run_uuid == run_id, Run.status.in_(_UNFINISHED)
        )
        return list((await self._session.execute(stmt)).scalars())
