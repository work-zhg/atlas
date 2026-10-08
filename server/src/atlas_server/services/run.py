"""Run 服务：创建、查询、取消、SSE 事件流（文档 §10 / §11.3）。

创建与订阅刻意拆成两个请求：
  POST /threads/{id}/runs    → 立刻返回 run_id
  GET  /threads/{id}/events  → SSE，可带 Last-Event-ID 重连
一次性响应流扛不住刷新页面、切后台、网络抖动、多标签页这几件事（§10.1）。

★ 订阅单位是**会话**而不是 run（doc/detail/suspension.html §04）：一条流带着
  该会话下所有 run 的事件，含子智能体的。按 run 订阅在委派挂起期间是瞎的 ——
  父 run 挂起后父流不再产出任何事件，而子 run 的审批请求正发生在那段时间里。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_server.domain.events import TraceEvent

from ..config import Settings
from ..db.models import Run
from ..errors import Conflict, NotFound, ThreadLocked
from ..executor.base import RunExecutor
from ..repositories.agent import AgentRepository
from ..repositories.run import RunRepository
from ..repositories.thread import ThreadRepository
from ..schemas.run import RunAccepted, RunCreate, RunOut, RunTraceOut
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
        #
        # ★ 权威判据在 **DB**：这个会话上还有没有没跑完的 run。Redis 锁退到
        #   第二位，只防「两个请求同时进来」的竞态。
        #
        #   为什么要换。一个 run 可以在委派处挂起等上一小时，而挂起期间
        #   **没有进程**替它续租 Redis 锁 —— 锁按 TTL 到期，第二个 run 就能
        #   进来与它交错写同一个 thread，「同一会话串行」这条承诺静默地破了。
        #   把 TTL 一路调到覆盖最长等待也不行：进程被杀时锁要空悬那么久，
        #   对应会话在这段时间里一直 409。
        #
        #   DB 里的 run 状态没有这个问题 —— 它不会过期，也不依赖谁还活着。
        if (active := await self._runs.active_run_of(thread_id)) is not None:
            raise ThreadLocked(
                "该会话已有正在运行的 run，请等待其结束或先取消",
                thread_id=str(thread_id),
                run_id=str(active),
            )

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
        """Redis 锁的 TTL。

        ★ 它不再是串行的**权威** —— 那个判据搬到 DB 了（见 create 里的
          active_run_of）。这把锁现在只防一件事：两个请求在同一瞬间都查到
          「没有活跃 run」然后都往下走。那个窗口是毫秒级的。

          TTL 因此不再需要覆盖「run 可能的最长时长」（挂起的 run 可以等上
          一小时，没有任何 TTL 覆盖得了它）。保留一个宽松值只是让锁在进程
          被杀后自己消失，而真正的串行由 DB 守着。
        """
        limits = (version_spec or {}).get("limits") or {}
        try:
            timeout_s = int(limits.get("timeout_s") or 300)
        except (TypeError, ValueError):
            timeout_s = 300
        return max(timeout_s, self._settings.thread_lock_ttl_s) + 120

    # ------------------------------------------------------------------ 查询

    async def get(self, run_id: UUID) -> RunOut:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))
        return _to_out(run)

    async def trace(self, run_id: UUID) -> RunTraceOut:
        """一轮已结束后的过程轨迹 —— 历史对话里的工具、委派、审批靠它重现。"""
        if await self._runs.get(run_id) is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))
        rows = await self._runs.archived_run_trace(run_id)
        return RunTraceOut(
            data=[
                event_from_row(
                    run_id=row.run_id,
                    seq=row.seq,
                    thread_seq=row.thread_seq,
                    ts=row.ts,
                    type_=row.type,
                    depth=row.depth,
                    data=row.data,
                )
                for row in rows
            ]
        )

    async def cancel(self, run_id: UUID) -> RunOut:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"run {run_id} 不存在", run_id=str(run_id))
        if run.status in _TERMINAL_STATUSES:
            # ★ 父 run 已结束，子 run 却可能还在跑（例如父被重启回收、子还挂着）。
            #   页面上的「停止」针对的是父 run —— 直接 409 的话，用户永远停不掉那个子 run。
            children = await self._runs.unfinished_children_of(run_id)
            if not children:
                raise Conflict(f"run 已处于终止状态 {run.status!r}", status=run.status)
            for child_id in children:
                await self._executor.cancel(child_id)
            return _to_out(run)
        await self._executor.cancel(run_id)

        # ★ 级联取消还在跑的子 run。
        #
        #   父 run 挂起时**没有进程**在盯着子 run —— 原先那个「父的轮询循环
        #   顺手取消子 run」的路径不存在了。不级联的话子 run 会一路跑到自己
        #   的超时，而 acp 的子 run 整段时间都占着一个 Pod；更难解释的是它
        #   跑完之后还会来唤醒一个已经被取消的父 run。
        #
        #   ★ 对 running 的父 run 同样发一遍：重复取消是幂等的（取消位就是
        #     一个 Redis 键），而少发一次的代价是一个跑飞的子 run。
        for child_id in await self._runs.unfinished_children_of(run_id):
            await self._executor.cancel(child_id)

        return _to_out(run)

    # ------------------------------------------------------------------ SSE
    #
    # ★ 按 run 订阅的 `stream` 已删除（S5）。订阅单位是会话 —— 保留两条
    #   路径会让「哪条流是权威」有两个答案，而它们必然漂移：run 流不含
    #   子 run 的事件，于是委派挂起期间它什么都收不到。

    async def ensure_thread(self, thread_id: UUID) -> None:
        """会话不存在就抛 NotFound。

        ★ 供 SSE 路由在**构造流之前**调用：生成器里抛的异常已经在响应体里了，
          客户端拿到的是一个 200 的空流而不是 404（run 流那边同款处理）。
        """
        if await self._threads.get(thread_id) is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))

    async def stream_thread(
        self, thread_id: UUID, *, after_seq: int | None
    ) -> AsyncIterator[str]:
        """产出一条**会话**流的 SSE 帧。id 写 `thread_seq`。

        与按 run 订阅的 `stream` 有三处本质差别：

        ① **永不自动结束。** 会话流跟着会话活一辈子 —— 一轮跑完后面还有下
           一轮，中间还夹着挂起等待。终态事件不再是关闭信号，关闭的唯一理由
           是客户端断开。这也是子 run 的过程（含审批弹窗）能被看见的前提：
           父 run 挂起时父流不产出事件，但会话流还在。

        ② **归档与 Redis 接力。** Redis 的流有 maxlen 裁剪，而这条流可以很长
           —— 早期事件只在 Postgres 里。所以先读归档补到游标，再从 Redis 续，
           两段按 thread_seq 接上。

        ③ **首连要限量。** after_seq 为 None（首次订阅，没有游标）时只回放
           最近 N 条。一条会话可以有几百轮，从 0 开始回放会把前端灌死 ——
           这是 thread 流相对 run 流新增的风险，run 流天然只有一轮的量。

        ★ DB 访问集中在方法开头，随后立刻 `session.close()`：SSE 可以挂数小时，
          而请求级 session 的 teardown 要等响应结束。不主动还的话每个观看者占
          死一条连接（pool 20 条，约 20 个并发观看就让**所有** HTTP 请求排队）。
          会话流的观看时长比 run 流长得多，这条纪律因此更要紧。
        """
        cursor = (
            after_seq
            if after_seq is not None
            else await self._runs.thread_seq_window_floor(
                thread_id, keep=self._settings.sse_thread_replay_events
            )
        )

        # 归档段：一次取完（有 limit 兜底），随后连接就能还掉
        archived = [
            event_from_row(
                run_id=row.run_id,
                seq=row.seq,
                thread_seq=row.thread_seq,
                ts=row.ts,
                type_=row.type,
                depth=row.depth,
                data=row.data,
            )
            for row in await self._runs.archived_thread_events(thread_id, after_seq=cursor)
        ]
        await self._session.close()

        for event in archived:
            yield event.to_sse(thread_cursor=True)
            cursor = max(cursor, event.thread_seq)

        # Redis 段：从归档的末尾接上。
        # ★ 归档读完与 Redis 开始读之间的新事件不会丢 —— tail_thread 从流的
        #   头部 XRANGE 起，按 thread_seq 过滤，缺口由游标本身兜住。
        block_ms = self._settings.sse_heartbeat_s * 1000
        async for item in self._relay.tail_thread(
            thread_id, after_seq=cursor, block_ms=block_ms
        ):
            if item is None:
                yield ": ping\n\n"  # 防中间层判定空闲断连（§10.3）
                continue
            yield item.to_sse(thread_cursor=True)


__all__ = ["RunService", "TraceEvent"]
