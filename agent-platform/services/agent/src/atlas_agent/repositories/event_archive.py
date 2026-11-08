"""run_event 表：事件的历史（Run 执行器 · 一段的执行与落库）。

Redis 是实时通道，这里是历史：归档、接管时查哪些会话序号已经归档、Redis 的序号丢了时的
恢复水位，以及会话流和轨迹的回放读取。
"""

from __future__ import annotations

from uuid import UUID

from atlas_engine.contracts.events import TraceEvent
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Run, RunEvent
from .run import (
    _ARCHIVED_EXCLUDED,
)


class EventArchive:
    """事件归档：唯一接触 run_event 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def archived_events(self, run_id: UUID, *, after_seq: int) -> list[RunEvent]:
        stmt = (
            select(RunEvent)
            .where(RunEvent.run_uuid == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def archived_run_trace(self, run_id: UUID, *, limit: int = 5_000) -> list[RunEvent]:
        """一轮的完整归档轨迹：这个 run 自己的事件 + 它委派出去的子 run 的事件。

        ★ 子 run 靠 parent_run_id 找。委派深度结构性封顶 1，一层子查询就够。
        ★ 按 thread_seq 排：父子交错发生，thread_seq 才是它们在会话流上的真实顺序。
        ★ 归档被清理后这里返回空 —— 前端据此退回只显示正文。
        """
        children = select(Run.run_uuid).where(Run.parent_run_uuid == run_id)
        stmt = (
            select(RunEvent)
            .where((RunEvent.run_uuid == run_id) | RunEvent.run_uuid.in_(children))
            .order_by(RunEvent.thread_seq)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def archived_thread_events(
        self, thread_id: UUID, *, after_seq: int, limit: int = 5_000
    ) -> list[RunEvent]:
        """一条会话流上 thread_seq 之后的归档事件（跨 run，含子 run）。

        ★ 为什么会话流也要读归档：Redis 的流有 maxlen 裁剪，而会话流永不结束
          —— 活跃久了早期事件只在 Postgres 里。所以回放是「先归档、再 Redis」
          接力，两段按 thread_seq 接上（见 services/run.py::stream_thread）。

        ★ limit 是安全网而不是分页：正常路径由调用方先算出一个窗口下界
          （thread_seq_window_floor），不会一次要几千条。
        """
        stmt = (
            select(RunEvent)
            .where(RunEvent.thread_uuid == thread_id, RunEvent.thread_seq > after_seq)
            .order_by(RunEvent.thread_seq)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def thread_seq_window_floor(self, thread_id: UUID, *, keep: int) -> int:
        """「只回放最近 keep 条」对应的游标下界。

        ★ 为什么必须有这个窗口。会话流的订阅单位是 thread，而一条会话可以有
          几百轮 —— 首连若从 thread_seq=0 开始回放，打开一个老会话就是往前端
          灌几万个事件，页面直接卡死。这是 thread 流相对 run 流**新增**的风险
          （run 流天然只有一轮的量）。

        ★ 返回 0 = 这条会话的事件不足 keep 条，全给。
        """
        inner = (
            select(RunEvent.thread_seq)
            .where(RunEvent.thread_uuid == thread_id)
            .order_by(RunEvent.thread_seq.desc())
            .limit(keep)
            .subquery()
        )
        floor = (await self._session.execute(select(func.min(inner.c.thread_seq)))).scalar()
        # 下界是「最早那条的前一个」—— after_seq 语义是**严格大于**
        return max(int(floor) - 1, 0) if floor else 0

    async def archived_thread_seqs(self, run_id: UUID, thread_seqs: list[int]) -> set[int]:
        """这些 thread_seq 里哪些已经归档 —— 接管时从 Redis 补归档要跳过它们。"""
        if not thread_seqs:
            return set()
        stmt = select(RunEvent.thread_seq).where(
            RunEvent.run_uuid == run_id, RunEvent.thread_seq.in_(thread_seqs)
        )
        return set((await self._session.execute(stmt)).scalars())

    async def archive_events(self, events: list[TraceEvent], *, thread_id: UUID) -> None:
        """每段结束时批量落库 —— Redis 是实时通道，这里是历史（文档 §10.2）。

        thread_id 是**事件流归属的会话**（根会话，Thread.stream_thread_id），
        与 run.thread_id 在子 run 上是不同的值。

        ★ delta 类事件不归档（_ARCHIVED_EXCLUDED）。它们的价值是实时打字
          效果；回放历史时正文来自 message.completed 一次到位，而前端读
          历史本来就走 message 表。不滤的话一个长会话能到几十万行，其中
          绝大多数永远不会被读到。

        ★ 只落已盖过会话序号的事件。thread_seq 是主键的一半，为 0 说明它
          没经过 publish —— 那是个 bug，宁可少归档一条也不能让整批 INSERT
          撞主键（那会让整段的收尾失败，run 卡在 running）。
        """
        rows = [
            {
                "thread_uuid": thread_id,
                "thread_seq": e.thread_seq,
                "run_uuid": e.run_id,
                "seq": e.seq,
                "ts": e.ts,
                "type": e.type.value,
                "depth": e.depth,
                "data": e.data,
            }
            for e in events
            if e.type not in _ARCHIVED_EXCLUDED and e.thread_seq > 0
        ]
        if not rows:
            return
        await self._session.execute(insert(RunEvent), rows)

    async def thread_seq_floor(self, thread_id: UUID) -> int:
        """这条会话已归档的最大 thread_seq —— Redis 序号丢失时的恢复水位。

        ★ 每段执行开始时查一次，不是每个事件查一次。它只在 Redis 的序号 key
          不存在时才真正被用到（见 EventRelay.next_thread_seq 的 Lua）。
        """
        stmt = select(func.max(RunEvent.thread_seq)).where(RunEvent.thread_uuid == thread_id)
        return int((await self._session.execute(stmt)).scalar() or 0)
