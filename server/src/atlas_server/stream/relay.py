"""★ Redis Stream 事件中继（文档 §10.2）。

为什么必须有它：FastAPI 多 worker 部署下，`POST /runs` 可能落在 worker A，
而浏览器的 `GET /threads/{id}/events` 落在 worker B。没有跨进程中继，B 拿不到
A 产出的事件。Redis Stream 同时解决了跨 worker 与断线重连两个问题。

★ 订阅单位是 **thread**，不是 run（doc/detail/suspension.html §04）。一个会话
  一条流，该会话下所有 run（含子 run）的事件都进去 —— 子 run 的过程与审批
  因此不再需要「冒泡到父流」那套转发。

读取是两段接力：
  1. 先从 Postgres 的 run_event 归档读到游标（Redis 流有 maxlen 裁剪，而会话流
     可以很长 —— 早期事件只在库里）
  2. 再从 Redis 续，两段按 thread_seq 接上

  归档那一段在 services/run.py::stream_thread 里（relay 不碰 DB）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from uuid import UUID

import redis.asyncio as aioredis

from atlas_server.domain.events import TERMINAL_EVENTS, EventType, TraceEvent

logger = logging.getLogger(__name__)

_TERMINAL_VALUES = {e.value for e in TERMINAL_EVENTS}


def thread_stream_key(thread_id: UUID) -> str:
    """一个会话一条流 —— 子 run 的事件也进它所属**根会话**的这条。"""
    return f"thread:events:{thread_id}"


def thread_seq_key(thread_id: UUID) -> str:
    return f"thread:seq:{thread_id}"


def cancel_key(run_id: UUID) -> str:
    return f"run:cancel:{run_id}"


def thread_lock_key(thread_id: UUID) -> str:
    return f"run:lock:{thread_id}"


def idempotency_key(key: str) -> str:
    return f"idem:run:{key}"


#: 会话级序号的分配。**必须原子** —— 见 next_thread_seq 的说明。
#:
#:   KEYS[1] = thread:seq:{id}
#:   ARGV[1] = DB 水位（Redis 里没有这个 key 时的起点）
#:   ARGV[2] = TTL 秒
_NEXT_THREAD_SEQ = (
    "if redis.call('EXISTS', KEYS[1]) == 0 then "
    "  redis.call('SET', KEYS[1], ARGV[1]) "
    "end "
    "local v = redis.call('INCR', KEYS[1]) "
    "redis.call('EXPIRE', KEYS[1], ARGV[2]) "
    "return v"
)

#: 序号 key 的 TTL。远长于事件流本身（run_events_ttl_s，默认 1 天）——
#: 它是会话的**水位**，会话还活着就不该丢。活跃会话每次分配都续期，
#: 所以实际只有冷启动/Redis 重建才会走到水位恢复。
_SEQ_TTL_S = 30 * 86_400

#: 一条会话流最多保留多少个事件。
#:
#: ★ thread 流**永不结束**（它跟着会话活一辈子），不设上限的话内存随会话
#:   寿命单调增长。用 approximate 裁剪（`~`）让 Redis 按整块丢，O(1)。
#:
#: ★ 被裁掉的事件仍在 Postgres 的归档表里（delta 除外，那些本来就不归档），
#:   回放走 §10.2 的情形 2。
_STREAM_MAXLEN = 10_000


class EventRelay:
    def __init__(self, redis: aioredis.Redis, *, ttl_s: int = 86_400) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    # ------------------------------------------------------------------ 序号

    async def next_thread_seq(self, thread_id: UUID, *, floor: int) -> int:
        """分配一个会话级序号。

        ★ 为什么不能在进程内数。同一条会话流有**多个并发生产者** ——
          并行委派的几个子 run 同时在写（它们的事件都进根会话那条流）。
          各自在内存里递增会撞号，而撞号的表现是前端把新事件当重复丢掉。

        ★ 为什么必须原子。Redis 是易失的：key 丢了会从 1 重来，于是整段
          事件的 seq 全部落在前端 `seq <= lastSeq` 的丢弃区里 —— **静默**
          故障，没有报错、没有缺口告警，只是界面再也不更新。
          「不存在就用水位初始化」和「递增」之间不能有窗口，所以走 Lua。

        floor: DB 里这条会话已有的最大 thread_seq。调用方每段查一次即可
            （不是每事件），见 executor 的 _thread_seq_floor。
        """
        value = await self._redis.eval(
            _NEXT_THREAD_SEQ, 1, thread_seq_key(thread_id), str(floor), str(_SEQ_TTL_S)
        )
        return int(value)

    # ------------------------------------------------------------------ 发布

    async def publish(self, event: TraceEvent, *, thread_id: UUID, floor: int = 0) -> TraceEvent:
        """发布一个事件，返回**盖了会话级序号**的那一份。

        ★ 返回值必须被调用方用上：归档要按 thread_seq 落库（它是主键的一半），
          而分配发生在这里。丢掉返回值的表现是归档全部撞在 thread_seq=0 上。
        """
        stamped = event.stamped(await self.next_thread_seq(thread_id, floor=floor))
        await self.emit(stamped, thread_id=thread_id)
        return stamped

    async def emit(self, event: TraceEvent, *, thread_id: UUID) -> None:
        """发布一个**已经盖过序号**的事件。

        ★ 收尾事件（终态 / 挂起）走这条而不是 publish：它的序号必须在**落库
          之前**就定下来（归档要按 thread_seq 落，而 thread_seq 是主键的一半），
          而发布必须在落库之后（「看到终止事件 ⇒ DB 已是最终状态」）。
          分配与发布因此要能分开做。
        """
        await self._emit(thread_stream_key(thread_id), event)

    async def _emit(self, key: str, event: TraceEvent) -> None:
        await self._redis.xadd(
            key,
            {
                "seq": str(event.seq),
                "thread_seq": str(event.thread_seq),
                "type": event.type.value,
                "payload": event.model_dump_json(),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        # 每次都续期：run 可能跑很久，避免中途过期
        await self._redis.expire(key, self._ttl_s)

    # ------------------------------------------------------------------ 会话流

    async def tail_thread(
        self, thread_id: UUID, *, after_seq: int, block_ms: int
    ) -> AsyncIterator[TraceEvent | None]:
        """从 after_seq 之后持续产出一条**会话**流上的事件。

        ★ 与 `tail` 最重要的差别：**不因终态事件而结束**。会话流跟着会话活
          一辈子 —— 一轮跑完了后面还会有下一轮，中间还夹着挂起等待。遇到
          run.finished 就 return 的话，用户发第二句话时流已经断了。
          结束的唯一理由是客户端断开（迭代被取消）。

        ★ 按 thread_seq 过滤而不是按 Redis 的 entry id：调用方可能已经从
          归档表读过一段（Redis 的流有 maxlen，早期事件只在 Postgres 里），
          两段要能按同一个游标接上。
        """
        key = thread_stream_key(thread_id)
        last_id = "0-0"

        # 先把流里已有的读完，同时把游标推到末尾
        for entry_id, fields in await self._redis.xrange(key):
            last_id = entry_id
            if int(fields.get("thread_seq", 0)) > after_seq:
                yield TraceEvent.model_validate_json(fields["payload"])

        while True:
            resp = await self._redis.xread({key: last_id}, block=block_ms, count=100)
            if not resp:
                yield None  # 心跳，防中间层判定空闲断连
                continue
            for _key, entries in resp:
                for entry_id, fields in entries:
                    last_id = entry_id
                    if int(fields.get("thread_seq", 0)) > after_seq:
                        yield TraceEvent.model_validate_json(fields["payload"])

    # ------------------------------------------------------------------ 取消

    async def request_cancel(self, run_id: UUID) -> None:
        await self._redis.set(cancel_key(run_id), "1", ex=3600)

    async def is_cancelled(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(cancel_key(run_id)))

    async def clear_cancel(self, run_id: UUID) -> None:
        await self._redis.delete(cancel_key(run_id))

    # ------------------------------------------------------------------ 串行锁

    #: 只删自己持有的锁（compare-and-delete 必须原子，GET+DEL 两步之间
    #: 锁可能过期又被新 run 拿走）
    _RELEASE_IF_OWNER = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end "
        "return 0"
    )

    async def acquire_thread_lock(self, thread_id: UUID, *, owner: UUID, ttl_s: int) -> bool:
        """同一会话同时只允许一个 run，防止并发写坏 state。

        锁值写 owner（= run_id）：TTL 意外过期、锁被下一个 run 拿走后，
        旧 run 结束时不能把新 run 的锁误删掉 —— 那会把「串行」的窗口
        再撕开一次。
        """
        return bool(
            await self._redis.set(thread_lock_key(thread_id), str(owner), nx=True, ex=ttl_s)
        )

    async def release_thread_lock(self, thread_id: UUID, *, owner: UUID) -> None:
        await self._redis.eval(self._RELEASE_IF_OWNER, 1, thread_lock_key(thread_id), str(owner))

    # ------------------------------------------------------------------ 幂等

    async def remember_idempotency(self, key: str, run_id: UUID, *, ttl_s: int = 86_400) -> None:
        await self._redis.set(idempotency_key(key), str(run_id), ex=ttl_s)

    async def lookup_idempotency(self, key: str) -> UUID | None:
        found = await self._redis.get(idempotency_key(key))
        return UUID(found) if found else None


class RedisCancelToken:
    """注入给 engine 的取消信号（engine 不认识 Redis，只认这个协议）。"""

    def __init__(self, relay: EventRelay, run_id: UUID) -> None:
        self._relay = relay
        self._run_id = run_id

    async def is_cancelled(self) -> bool:
        try:
            return await self._relay.is_cancelled(self._run_id)
        except Exception:  # Redis 抖动不该让整个 run 挂掉
            logger.warning("取消信号检查失败，按未取消处理", exc_info=True)
            return False


def event_from_row(
    *,
    run_id: UUID,
    seq: int,
    ts,
    type_: str,
    depth: int,
    data: dict,
    thread_seq: int = 0,
) -> TraceEvent:
    """Postgres 归档行 → TraceEvent（超过 Redis TTL 的历史回放）。"""
    return TraceEvent(
        seq=seq,
        thread_seq=thread_seq,
        run_id=run_id,
        ts=ts,
        type=EventType(type_),
        depth=depth,
        data=data,
    )
