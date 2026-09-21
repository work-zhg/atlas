"""会话与消息服务（文档 §11.2）。"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Agent, AgentVersion, Thread
from ..errors import BadCursor, NotFound
from ..pagination import InvalidCursor, decode_cursor, encode_cursor
from ..repositories.agent import AgentRepository
from ..providers.filesystem import make_workspace
from ..providers.filesystem.skill_copy import seed_session_skills, skill_metas_for
from ..providers.cluster import ClusterPods
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..schemas.agent import AgentSpecIn
from ..schemas.thread import (
    MessageListOut,
    MessageOut,
    ThreadCreate,
    ThreadListOut,
    ThreadOut,
    ThreadUpdate,
)


def _to_out(
    thread: Thread,
    agent: Agent,
    *,
    subagent_thread_count: int = 0,
    active_run_id: UUID | None = None,
) -> ThreadOut:
    return ThreadOut(
        id=thread.id,
        agent_id=agent.id,
        agent_slug=agent.slug,
        agent_name=agent.name,
        agent_avatar_key=agent.avatar_key,
        title=thread.title,
        title_source=thread.title_source,  # type: ignore[arg-type]
        status=thread.status,  # type: ignore[arg-type]
        parent_thread_id=thread.parent_thread_id,
        subagent_name=thread.subagent_name,
        subagent_thread_count=subagent_thread_count,
        active_run_id=active_run_id,
        message_count=thread.message_count,
        compact_count=thread.compact_count,
        latest_state=thread.latest_state,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _parse_cursor(cursor: str | None):
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except InvalidCursor as exc:
        raise BadCursor(str(exc)) from exc


logger = logging.getLogger(__name__)


class ThreadService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repo = ThreadRepository(session)
        self._agents = AgentRepository(session)

    # ------------------------------------------------------------------ 读

    async def list(self, *, status: str | None, cursor: str | None, limit: int) -> ThreadListOut:
        rows = await self._repo.list(status=status, cursor=_parse_cursor(cursor), limit=limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor(rows[-1][0].updated_at, rows[-1][0].id) if has_more and rows else None
        )
        return ThreadListOut(data=[_to_out(t, a) for t, a in rows], next_cursor=next_cursor)

    async def get(self, thread_id: UUID) -> ThreadOut:
        thread, agent = await self._require(thread_id)
        # 只在详情上算：删除确认要显示会被一起删掉的子会话数（设计 §12）。
        return _to_out(
            thread,
            agent,
            subagent_thread_count=await self._repo.count_subagent_threads(thread_id),
            # 刷新后恢复事件流的入口（列表接口不算，避免 N+1）
            active_run_id=await RunRepository(self._session).active_run_of(thread_id),
        )

    async def list_messages(
        self, thread_id: UUID, *, cursor: str | None, limit: int
    ) -> MessageListOut:
        await self._require(thread_id)
        rows = await self._repo.list_messages(
            thread_id, cursor=_parse_cursor(cursor), limit=limit + 1
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        return MessageListOut(
            data=[
                MessageOut(
                    id=m.id,
                    thread_id=m.thread_id,
                    run_id=m.run_id,
                    role=m.role,  # type: ignore[arg-type]
                    content=m.content,
                    created_at=m.created_at,
                )
                for m in rows
            ],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------ 写

    async def create(self, payload: ThreadCreate, *, user_id: UUID) -> ThreadOut:
        found = await self._agents.get(payload.agent_id)
        if found is None:
            raise NotFound(f"智能体 {payload.agent_id} 不存在", agent_id=str(payload.agent_id))
        agent, version = found
        thread = await self._repo.create(agent_id=agent.id, title=payload.title, created_by=user_id)
        await self._session.refresh(thread)
        await self._seed_skills(version, thread_id=thread.id, user_id=user_id)
        return _to_out(thread, agent)

    async def _seed_skills(self, version: AgentVersion, *, thread_id: UUID, user_id: UUID) -> None:
        """把 agent 引用的技能拷进本会话的 skills 前缀。

        ★ 时机是**会话创建**，不是首次 run：用户对「新建会话」的延迟容忍度
          更高，而首轮对话的等待是最刺眼的。

        ★ 失败不吞。少拷几个文件就默默开始的话，表现是模型「照技能说的做了
          但脚本不存在」—— 比明确报错难查得多。异常冒泡会让整个请求事务
          回滚，于是**会话根本不会建出来**，而不是建出一个技能残缺的会话。

        没配对象存储时 make_workspace 返回 None —— 那是「没有文件能力」这个
        产品决定的一部分，技能一并不投送（模型那边文件工具也一个都不注册）。
        """
        spec = AgentSpecIn.model_validate(version.spec)
        if not spec.skills:
            return
        fs = make_workspace(self._settings, user_id, thread_id)
        if fs is None:
            logger.info("未配置对象存储，跳过技能投送 thread=%s", thread_id)
            return
        metas = skill_metas_for([s.to_engine() for s in spec.skills])
        await seed_session_skills(fs, metas)
        logger.info("会话 %s 投送了 %d 个技能", thread_id, len(metas))

    async def update(self, thread_id: UUID, payload: ThreadUpdate) -> ThreadOut:
        thread, agent = await self._require(thread_id)
        if payload.title is not None:
            thread.title = payload.title
            # ★ 用户改过的标题永不被自动生成覆盖（决策 5）
            thread.title_source = "manual"
        if payload.status is not None:
            thread.status = payload.status
        await self._session.flush()
        await self._session.refresh(thread)
        return _to_out(thread, agent)

    async def delete(self, thread_id: UUID) -> None:
        thread, _ = await self._require(thread_id)
        await self._repo.delete(thread)
        # Pod 级联：清理失败不该挡住会话删除 —— 残留由 cluster 的 reap 兜底
        # （启动时按「还活着的会话」名单清）。
        await ClusterPods(self._settings).release(str(thread_id))

    # ------------------------------------------------------------------ 内部

    async def _require(self, thread_id: UUID) -> tuple[Thread, Agent]:
        found = await self._repo.get(thread_id)
        if found is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        return found
