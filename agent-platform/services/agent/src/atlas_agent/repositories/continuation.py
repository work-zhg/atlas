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

        ★ `waiting_on IS NOT NULL` 这一条不是优化，是**正确性**。
          `awaiting_approval` 这个状态值在两条路径上含义不同：

            native   图已跳出、进程已结束 —— 真挂起，waiting_on 有值
            acp      CLI 在 Pod 里发起的同步 RPC，**run 还在跑** —— bridge 正
                     等着响应，所以只能在线等（acp/runtime.py::_await_decision），
                     这期间 gate 也会把 run 标成 awaiting_approval，但它没有
                     waiting_on

          不加这一条的话，扫描会把一个**正在跑**的 acp 子 run 当成可续跑，
          submit 第二次 —— 第二条上游连接 连上同一个 Pod，bridge 按
          CLOSE_SUPERSEDED(4409) 关掉第一个，整轮以 runtime_crashed 失败。
          真机验证时撞到过，症状是「审批批准了，子 run 却崩了」。
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
