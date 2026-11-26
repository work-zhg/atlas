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
from typing import Any
from uuid import UUID

from atlas_engine.contracts.events import TERMINAL_EVENTS, EventType, TraceEvent
from atlas_redis import RedisClient, ScriptRegistry
from atlas_redislock import RedisLock

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

#: 重连时读流里已有事件的每页条数
_REPLAY_PAGE = 500


_SCRIPT_NEXT_SEQ = "atlas_agent.relay.next_thread_seq"
_SCRIPT_RELEASE_LOCK = "atlas_agent.relay.release_if_owner"
_SCRIPT_RENEW_LOCK = "atlas_agent.relay.renew_if_owner_or_free"


class EventRelay:
    def __init__(self, redis: Any, *, ttl_s: int = 86_400) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        # ★ 接入 atlas_redis.RedisClient 时通过 ScriptRegistry 走 EVALSHA，
        #   测试传原始假客户端时自动回落原始 eval，保持 100% 兼容
        self._scripts: ScriptRegistry | None = None
        if isinstance(redis, RedisClient):
            reg = ScriptRegistry(redis)
            reg.register(_SCRIPT_NEXT_SEQ, _NEXT_THREAD_SEQ)
            reg.register(_SCRIPT_RELEASE_LOCK, self._RELEASE_IF_OWNER)
            reg.register(_SCRIPT_RENEW_LOCK, self._RENEW_IF_OWNER_OR_FREE)
            self._scripts = reg

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
        if self._scripts is not None:
            value = await self._scripts.run(
                _SCRIPT_NEXT_SEQ,
                keys=(thread_seq_key(thread_id),),
                args=(str(floor), str(_SEQ_TTL_S)),
                key_pattern="agent:thread:seq",
            )
            return int(value)
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
        # ★ XADD 和续期一次往返发出去（pipeline，不开事务）：流式输出时每个 token 一个事件，
        #   两条命令各等一次往返就是每个 token 多一次。续期每次都做：run 可能跑很久，避免中途过期
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.xadd(
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
            pipe.expire(key, self._ttl_s)
            await pipe.execute()

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

        # ★ 末尾快跳（c4-model/infra-platform/design-redis.html）：客户端重连时游标大多已在流尾，
        #   先 XREVRANGE COUNT 1 看流尾序号，<= after_seq 时直接从流尾进 XREAD，O(1) 跳过全流扫描
        need_replay = True
        if hasattr(self._redis, "xrevrange"):
            latest = await self._redis.xrevrange(key, max="+", min="-", count=1)
            if not latest:
                need_replay = False
            else:
                tip_id, tip_fields = latest[0]
                if int(tip_fields.get("thread_seq", 0)) <= after_seq:
                    last_id = tip_id
                    need_replay = False

        # 先把流里已有的读完，同时把游标推到末尾。★ 分页读：避免一次拉 1 万条内存尖峰
        if need_replay:
            start = "-"
            while True:
                page = await self._redis.xrange(key, min=start, count=_REPLAY_PAGE)
                for entry_id, fields in page:
                    last_id = entry_id
                    if int(fields.get("thread_seq", 0)) > after_seq:
                        try:
                            yield TraceEvent.model_validate_json(fields["payload"])
                        except Exception:
                            logger.warning(
                                "会话流回放跳过损坏事件：thread=%s entry=%s",
                                thread_id,
                                entry_id,
                                exc_info=True,
                            )
                if len(page) < _REPLAY_PAGE:
                    break
                start = f"({last_id}"  # 不含上一页的最后一条

        while True:
            resp = await self._redis.xread({key: last_id}, block=block_ms, count=100)
            if not resp:
                yield None  # 心跳，防中间层判定空闲断连
                continue
            for _key, entries in resp:
                for entry_id, fields in entries:
                    last_id = entry_id
                    if int(fields.get("thread_seq", 0)) > after_seq:
                        try:
                            yield TraceEvent.model_validate_json(fields["payload"])
                        except Exception:
                            logger.warning(
                                "会话流实时跳过损坏事件：thread=%s entry=%s",
                                thread_id,
                                entry_id,
                                exc_info=True,
                            )

    # ------------------------------------------------------------------ 取消

    async def request_cancel(self, run_id: UUID) -> None:
        await self._redis.set(cancel_key(run_id), "1", ex=3600)

    async def is_cancelled(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(cancel_key(run_id)))

    async def clear_cancel(self, run_id: UUID) -> None:
        await self._redis.delete(cancel_key(run_id))

    # ------------------------------------------------------------------ 串行锁

    def subagent_admission_lock(self, tenant: UUID) -> RedisLock:
        """派 SubAgent 的准入锁：同一租户里「数在跑的子 run → 建子 run」同一时刻只有一个在做。

        ★ 不锁的话准入是软上限：同一批并发派出的几个 SubAgent 先后数到的都是派之前的数，
          一起通过。持有时间只有一次计数加一次插入；计数按租户（run 表自动按租户过滤），锁也按租户。
        """
        return RedisLock(self._redis, f"run:subagent-admission:{tenant}", ttl_s=30)

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
        if self._scripts is not None:
            await self._scripts.run(
                _SCRIPT_RELEASE_LOCK,
                keys=(thread_lock_key(thread_id),),
                args=(str(owner),),
                key_pattern="agent:thread:lock",
            )
            return
        await self._redis.eval(self._RELEASE_IF_OWNER, 1, thread_lock_key(thread_id), str(owner))

    #: 续期（或在已过期时重新占上）—— 只要锁不在**别的** run 手里
    _RENEW_IF_OWNER_OR_FREE = (
        "local v = redis.call('get', KEYS[1]) "
        "if v == false or v == ARGV[1] then "
        "  redis.call('set', KEYS[1], ARGV[1], 'PX', ARGV[2]) return 1 "
        "end "
        "return 0"
    )

    async def renew_thread_lock(self, thread_id: UUID, *, owner: UUID, ttl_s: float) -> bool:
        """会话锁跟随 run 的租约（横向扩展设计 §4.4）：持租约的执行器每次续租顺带续它。

        ★ 锁已过期就重新占上（owner 仍是同一个 run）：接管者领取时锁多半已经过期了。
          被别的 run 占着返回 False —— 那不该发生（DB 里同一会话只有一个未结束的 run），
          只记日志，不影响执行：串行的权威在 DB。
        """
        if self._scripts is not None:
            return bool(
                await self._scripts.run(
                    _SCRIPT_RENEW_LOCK,
                    keys=(thread_lock_key(thread_id),),
                    args=(str(owner), str(int(ttl_s * 1000))),
                    key_pattern="agent:thread:lock",
                )
            )
        return bool(
            await self._redis.eval(
                self._RENEW_IF_OWNER_OR_FREE,
                1,
                thread_lock_key(thread_id),
                str(owner),
                str(int(ttl_s * 1000)),
            )
        )

    # ------------------------------------------------------------------ 接管

    async def run_events_after(
        self, thread_id: UUID, run_id: UUID, *, after_seq: int
    ) -> list[TraceEvent]:
        """会话流里这个 run 在 seq 之后的事件 —— 接管时找「发布了但没归档」的那段（§5.3）。

        ★ 整条流扫一遍（有 maxlen 封顶），只在接管时发生，不在热路径上。
        """
        out: list[TraceEvent] = []
        for _entry_id, fields in await self._redis.xrange(thread_stream_key(thread_id)):
            try:
                event = TraceEvent.model_validate_json(fields["payload"])
            except Exception:  # 坏条目不该挡住接管
                continue
            if event.run_id == run_id and event.seq > after_seq:
                out.append(event)
        return out

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
