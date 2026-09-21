"""Run 服务：创建、查询、取消、SSE 事件流（文档 §10 / §11.3）。

创建与订阅刻意拆成两个请求：
  POST /threads/{id}/runs  → 立刻返回 run_id
  GET  /runs/{id}/events   → SSE，可带 Last-Event-ID 重连
一次性响应流扛不住刷新页面、切后台、网络抖动、多标签页这几件事（§10.1）。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from atlas_server.domain.events import TraceEvent
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Run
from ..errors import Conflict, NotFound, ThreadLocked
from ..executor.base import RunExecutor
from ..repositories.agent import AgentRepository
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..schemas.run import RunAccepted, RunCreate, RunOut
from ..stream.relay import EventRelay, event_from_row

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def _to_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        thread_id=run.thread_id,
        parent_run_id=run.parent_run_id,
        status=run.status,  # type: ignore[arg-type]
        error_kind=run.error_kind,
        error_message=run.error_message,
        last_seq=run.last_seq,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        thinking_tokens=run.thinking_tokens,
        total_tokens=run.total_tokens,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
    )


class RunService:
    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        settings: Settings,
        executor: RunExecutor,
    ) -> None:
        self._session = session
        self._settings = settings
        self._executor = executor
        self._runs = RunRepository(session)
        self._threads = ThreadRepository(session)
        self._agents = AgentRepository(session)
        self._relay = EventRelay(redis, ttl_s=settings.run_events_ttl_s)

    # ------------------------------------------------------------------ 创建

    async def create(
        self, thread_id: UUID, payload: RunCreate, *, idempotency_key: str | None
    ) -> RunAccepted:
        if idempotency_key:
            existing = await self._relay.lookup_idempotency(idempotency_key)
            if existing is not None:
                run = await self._runs.get(existing)
                if run is not None:
                    # 移动端抖动导致的重复提交不该变成两条消息、跑两次
                    return RunAccepted(
                        run_id=run.id,
                        message_id=UUID(int=0),
                        status=run.status,  # type: ignore[arg-type]
                    )

        found = await self._threads.get(thread_id)
        if found is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        _thread, agent = found

        agent_row = await self._agents.get(agent.id)
        if agent_row is None or agent_row[0].current_version_id is None:
            raise NotFound(f"智能体 {agent.id} 无可用版本", agent_id=str(agent.id))
        version = agent_row[1]

        # 同一会话串行，防止并发 run 撕裂 state（§9）。
        # run_id 预生成：它同时是锁的 owner 值，而锁必须先于 run 行拿到。
        run_id = uuid4()
        locked = await self._relay.acquire_thread_lock(
            thread_id, owner=run_id, ttl_s=self._lock_ttl_s(version.spec)
        )
        if not locked:
            raise ThreadLocked(
                "该会话已有正在运行的 run，请等待其结束或先取消",
                thread_id=str(thread_id),
            )

        try:
            message = await self._threads.add_message(
                thread_id=thread_id, role="user", content=payload.content
            )
            run = await self._runs.create(
                run_id=run_id, thread_id=thread_id, agent_version_id=version.id
            )
            await self._session.commit()
        except Exception:
            await self._relay.release_thread_lock(thread_id, owner=run_id)
            raise

        if idempotency_key:
            await self._relay.remember_idempotency(idempotency_key, run.id)

        await self._executor.submit(run.id)
        return RunAccepted(run_id=run.id, message_id=message.id, status="queued")

    def _lock_ttl_s(self, version_spec: dict | None) -> int:
        """锁 TTL 必须覆盖 run 可能的最长时长，含审批等待。

        原先的固定 360s 是个 bug：approval_timeout_s=600 比它长 —— run 还在跑，
        锁先静默过期，第二个 run 进来与它交错写同一 thread，「同一会话串行」
        的承诺就破了。余量给收尾（persist/归档）。进程被杀时锁靠 TTL +
        启动时 reap_orphans 双路回收。

        ★ 接 K8s Pod 执行环境时这里要加一项：Pod 调度 + 镜像拉取可达分钟级，
          acp 的一轮比 native 长得多（acp 详设 §11 的 acp_* 超时）。
        """
        limits = (version_spec or {}).get("limits") or {}
        try:
            timeout_s = int(limits.get("timeout_s") or 300)
        except (TypeError, ValueError):
            timeout_s = 300
        return max(timeout_s, self._settings.approval_timeout_s) + 120

    # ------------------------------------------------------------------ 查询

    async def get(self, run_id: UUID) -> RunOut:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))
        return _to_out(run)

    async def cancel(self, run_id: UUID) -> RunOut:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))
        if run.status in _TERMINAL_STATUSES:
            raise Conflict(f"run 已处于终止状态 {run.status!r}", status=run.status)
        await self._executor.cancel(run_id)
        return _to_out(run)

    # ------------------------------------------------------------------ SSE

    async def stream(self, run_id: UUID, *, after_seq: int) -> AsyncIterator[str]:
        """产出 SSE 帧。id 写 TraceEvent.seq —— 浏览器重连自动带 Last-Event-ID。

        ★ DB 访问全部集中在方法开头，随后立刻 `session.close()` 把连接还给
          连接池。SSE 连接可以挂数小时，而请求级 session 的 teardown 要等
          响应结束才执行 —— 不主动还的话每个观看者占死一条连接
          （pool_size + max_overflow = 20，约 20 个并发观看就耗尽连接池，
          之后**所有** HTTP 请求排队等连接）。
        """
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))

        # ★ run 已终止：事件集是完整且不可变的 —— 补发缺口后**立即关闭**，绝不阻塞。
        #   否则"跑完之后刷新页面"（前端最常见的动作之一）会拿到一个挂住的连接：
        #   没有新事件可读，服务端却一直卡在 XREAD BLOCK 发心跳，直到客户端超时。
        replay_frames: list[str] | None = None
        if run.status in _TERMINAL_STATUSES:
            if await self._relay.has_data(run_id):
                events = await self._relay.replay(run_id, after_seq=after_seq)
                replay_frames = [event.to_sse() for event in events]
            else:
                # Redis 已过期 → 从 Postgres 归档回放（§10.2 情形 2）
                replay_frames = [
                    event_from_row(
                        run_id=run_id,
                        seq=row.seq,
                        ts=row.ts,
                        type_=row.type,
                        depth=row.depth,
                        data=row.data,
                    ).to_sse()
                    for row in await self._runs.archived_events(run_id, after_seq=after_seq)
                ]

        # 从这里起只剩 Redis —— 归还连接。teardown 里的 commit 对空事务是 no-op。
        await self._session.close()

        if replay_frames is not None:
            for frame in replay_frames:
                yield frame
            return

        block_ms = self._settings.sse_heartbeat_s * 1000
        async for item in self._relay.tail(run_id, after_seq=after_seq, block_ms=block_ms):
            if item is None:
                yield ": ping\n\n"  # 防中间层判定空闲断连（§10.3）
                continue
            yield item.to_sse()

    # ------------------------------------------------------------------ 启动清理

    async def reap_orphans(self) -> int:
        """进程内执行的代价：重启后残留的 running/queued 是孤儿（§12.1 / R2）。

        标成 interrupted，前端显示"因服务重启中断，可重试"，
        而不是让会话永远停在"运行中"。
        """
        rows = await self._runs.active_runs()
        count = await self._runs.mark_interrupted([run_id for run_id, _ in rows])
        if count:
            await self._session.commit()
            logger.warning("启动时回收了 %d 个孤儿 run", count)
        # 锁随孤儿一起释放：进程被杀时执行器的 finally 没跑到，锁只能等 TTL ——
        # 而 TTL 按最长 run 时长算（可达 30min+），不清的话重启后这段时间内
        # 对应会话一直 409。owner 校验保证不会误删重启后新起 run 的锁。
        for run_id, thread_id in rows:
            with contextlib.suppress(Exception):
                await self._relay.release_thread_lock(thread_id, owner=run_id)
        return count


__all__ = ["RunService", "TraceEvent"]
