from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Agent, Message, Run, Thread
from ..domain.messages import KIND_CHAT, StoredMessage


class ThreadRepository:
    """唯一接触 thread / message 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 读

    async def by_id(self, thread_id: UUID) -> Thread | None:
        """只要会话行本身，不 join agent。

        调用方（续跑时解析委派结论）要的只是 subagent_name —— 为它多 join
        一张表是白付一次连接开销。
        """
        return await self._session.get(Thread, thread_id)

    async def get(self, thread_id: UUID) -> tuple[Thread, Agent] | None:
        stmt = (
            select(Thread, Agent)
            .join(Agent, Agent.id == Thread.agent_id)
            .where(Thread.id == thread_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def list(
        self,
        *,
        owner: UUID | None = None,
        status: str | None = None,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 30,
    ) -> list[tuple[Thread, Agent]]:
        # ★ 只列用户会话。子会话是父会话的一部分（它的 message 是委派的
        #   中间过程），出现在会话列表里等于把内部过程当成用户的对话。
        stmt = (
            select(Thread, Agent)
            .join(Agent, Agent.id == Thread.agent_id)
            .where(Thread.parent_thread_id.is_(None))
        )
        if owner is not None:  # 只列本主体的会话（权限设计 §7.4）
            stmt = stmt.where(Thread.created_by == owner)
        if status:
            stmt = stmt.where(Thread.status == status)
        if cursor is not None:
            ts, last_id = cursor
            # keyset：(updated_at, id) 严格小于游标
            stmt = stmt.where(
                or_(
                    Thread.updated_at < ts,
                    and_(Thread.updated_at == ts, Thread.id < last_id),
                )
            )
        stmt = stmt.order_by(Thread.updated_at.desc(), Thread.id.desc()).limit(limit)
        return [(r[0], r[1]) for r in (await self._session.execute(stmt)).all()]

    async def list_messages(
        self,
        thread_id: UUID,
        *,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> list[Message]:
        """倒序：最新的在前，前端向上滚加载更旧的。

        ★ 只列 kind='chat'。工具结果（kind='tool_result'）是 role='user' 的
          消息，放进来前端会渲染成一串**空的用户气泡** —— 它的 content 里
          只有 tool_result block，而 MessageStream 的 blocksOf 只认 text。
          工具调用的展示本来就走事件流（Inspector 的「工具」页），不走这里。
        """
        stmt = select(Message).where(
            Message.thread_id == thread_id, Message.kind == KIND_CHAT
        )
        if cursor is not None:
            ts, last_id = cursor
            stmt = stmt.where(
                or_(
                    Message.created_at < ts,
                    and_(Message.created_at == ts, Message.id < last_id),
                )
            )
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())

    # ------------------------------------------------------------------ 写

    async def live_ids(self) -> list[UUID]:
        """沙箱回收眼里「还活着」的会话：没归档的，加上归档了但还有没结束的 run 的。

        ★ 归档的会话不算活：task 的 fresh=true 丢弃的子会话不会再被复用，它的沙箱（ACP Pod）
          不回收就一直占着配额。用户手动恢复归档会话时，下一轮重新建沙箱（CLI 从 /state resume）。
        """
        from .run import _UNFINISHED

        unfinished = select(Run.thread_id).where(Run.status.in_(_UNFINISHED))
        stmt = select(Thread.id).where(or_(Thread.status != "archived", Thread.id.in_(unfinished)))
        return list((await self._session.execute(stmt)).scalars())

    async def create(
        self,
        *,
        agent_id: UUID,
        title: str,
        created_by: UUID,
        parent_thread_id: UUID | None = None,
        subagent_name: str | None = None,
        status: str = "active",
        git_profile: str | None = None,
        source_app: str | None = None,
        memory: str = "on",
    ) -> Thread:
        root_thread_id = None
        if parent_thread_id is not None:
            # ★ SubAgent 会话继承父会话的 git 凭据档案、来源应用与记忆开关：
            #   派出去的 CLI 才是真正在 Pod 里跑 git 的那个（如原生 agent 派给
            #   cli-hand）；父会话关了记忆，子会话不能偷偷开着。
            parent = (
                await self._session.execute(
                    select(
                        Thread.git_profile, Thread.source_app, Thread.memory, Thread.root_thread_id
                    ).where(Thread.id == parent_thread_id)
                )
            ).one_or_none()
            # 会话树的根：父会话的根，父会话自己是根就是父会话（SubAgent 可以层层派下去）
            root_thread_id = parent_thread_id
            if parent is not None:
                if git_profile is None:
                    git_profile = parent.git_profile
                source_app, memory = parent.source_app, parent.memory
                root_thread_id = parent.root_thread_id or parent_thread_id
        thread = Thread(
            agent_id=agent_id,
            title=title,
            # 用户建会话时给了标题就算 manual —— 自动生成永不覆盖（决策 5）
            title_source="manual" if title else "pending",
            status=status,
            created_by=created_by,
            parent_thread_id=parent_thread_id,
            root_thread_id=root_thread_id,
            subagent_name=subagent_name,
            git_profile=git_profile,
            source_app=source_app,
            memory=memory,
        )
        self._session.add(thread)
        await self._session.flush()
        # server_default 列（created_at / updated_at / latest_state ...）需显式 refresh，
        # 否则同步读取触发隐式 IO → MissingGreenlet
        await self._session.refresh(thread)
        return thread

    # ------------------------------------------------------------------ 子会话

    async def find_subagent(self, parent_thread_id: UUID, name: str) -> Thread | None:
        """按 (父会话, 子智能体名) 找已有的子会话 —— 恢复的查找键。"""
        stmt = select(Thread).where(
            Thread.parent_thread_id == parent_thread_id,
            Thread.subagent_name == name,
            Thread.status == "active",
        )
        return (await self._session.execute(stmt)).scalars().one_or_none()

    async def archive_subagent(self, thread: Thread) -> None:
        """归档一个子会话，释放它占用的查找键。

        ★ 归档而不是删：历史不该因为一次 `fresh=True` 重来就消失（设计 §04）。
          `subagent_name` **保留** —— 它是执行器派生子智能体 spec 的依据，
          归档后的 run 仍要能回答「这是谁的会话」。释放查找键靠的是
          status：部分唯一索引与 find_subagent 都只认 active。
        """
        thread.status = "archived"
        await self._session.flush()

    async def count_subagent_threads(self, parent_thread_id: UUID) -> int:
        """父会话下的子会话数 —— 删除确认要显示它（设计 §12）。"""
        stmt = (
            select(func.count())
            .select_from(Thread)
            .where(Thread.parent_thread_id == parent_thread_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def add_message(
        self,
        *,
        thread_id: UUID,
        role: str,
        content: list[dict],
        run_id: UUID | None = None,
        kind: str = KIND_CHAT,
    ) -> Message:
        message = Message(
            thread_id=thread_id, role=role, content=content, run_id=run_id, kind=kind
        )
        self._session.add(message)
        await self._session.flush()
        await self._session.refresh(message)
        return message

    async def add_messages(
        self,
        *,
        thread_id: UUID,
        messages: Sequence[StoredMessage],
        run_id: UUID | None = None,
    ) -> int:
        """批量写入一轮产出的完整消息序列。返回写入条数。

        ★ 时间戳**显式逐条递增**，不用列的 default。default 是
          `datetime.now(UTC)` 逐行求值，在快的机器上同一批里连续两次调用
          可以落在同一微秒 —— 而消息列表按 (created_at, id) 排序，并列时
          退化为按随机 UUID 排。后果是一条 assistant(tool_use) 可能排到它的
          tool_result 后面，下一轮重建历史时 provider 直接回 400
          （tool_result 找不到对应的 tool_use）。

          迁移 0003 已经为「同一事务内并列」改过一次时钟口径；那一次改的是
          **单条**插入，这里是同一批里的多条，要自己保证严格递增。
        """
        if not messages:
            return 0
        base = datetime.now(UTC)
        self._session.add_all(
            [
                Message(
                    thread_id=thread_id,
                    role=stored.role,
                    kind=stored.kind,
                    content=stored.content,
                    run_id=run_id,
                    created_at=base + timedelta(microseconds=index),
                )
                for index, stored in enumerate(messages)
            ]
        )
        await self._session.flush()
        return len(messages)

    async def set_generated_title(self, thread_id: UUID, *, title: str, degraded: bool) -> bool:
        """写入自动生成的标题（§8）。返回是否真的写入。

        ★ `title_source='manual'` 的一律跳过 —— 用户改过的标题被自动生成
          冲掉是很恼人的 bug，决策 5 里 title_source 这个字段就是为它存在的。
          条件写在 UPDATE 的 WHERE 里而不是先查后写：并发下先查后写会有窗口，
          用户恰好在这期间改名就被覆盖了。
        """
        stmt = (
            update(Thread)
            .where(Thread.id == thread_id, Thread.title_source != "manual")
            .values(title=title, title_source="fallback" if degraded else "generated")
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    async def count_messages(self, thread_id: UUID) -> int:
        """★ 与 list_messages 同口径：只数对话轮。

        工具结果也数进去的话，会话列表上的「N 条消息」会随工具调用次数
        暴涨 —— 一轮里调十次工具就多二十条，而用户只发了一句话。
        """
        stmt = select(func.count()).select_from(Message).where(
            Message.thread_id == thread_id, Message.kind == KIND_CHAT
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def delete(self, thread: Thread) -> None:
        """会话是真删：连同子会话，以及它们的 message / run / run_event / run_state /
        approval / run_file。

        ★ 迁移 0013 去掉了外键（引用完整性由代码保证），ON DELETE CASCADE 不复存在 ——
          只删 thread 行会留下大量孤儿行（残留的未结束 run 还会占用全局的子智能体名额）。
        与 agent 不同：会话没有"别的行还指着它"的问题。
        """
        from ..db.models import Approval, Message, Run, RunEvent, RunFile, RunState

        ids = [thread.id, *await self._session.scalars(
            select(Thread.id).where(Thread.parent_thread_id == thread.id)
        )]
        run_ids = select(Run.id).where(Run.thread_id.in_(ids)).scalar_subquery()
        for stmt in (
            delete(Approval).where(Approval.run_id.in_(run_ids)),
            delete(RunState).where(RunState.run_id.in_(run_ids)),
            delete(RunEvent).where(RunEvent.thread_id.in_(ids)),
            delete(RunFile).where(RunFile.thread_id.in_(ids)),
            delete(Message).where(Message.thread_id.in_(ids)),
            delete(Run).where(Run.thread_id.in_(ids)),
            delete(Thread).where(Thread.parent_thread_id == thread.id),
        ):
            await self._session.execute(stmt)
        await self._session.delete(thread)
        await self._session.flush()
