"""会话级序号的分配与恢复（S2）。

这个序号是 thread 流的游标，前端靠它去重（`seq <= lastSeq` 即丢弃）。
它的两个失效形态都是**静默**的 —— 没有报错、没有缺口告警，只是界面再也
不更新：

  撞号    两个并发生产者各自在内存里递增（并行委派的几个子 run 同时写同一
          条流），后来的那些落在前端的丢弃区里
  重来    Redis 的序号 key 丢了（重启/驱逐），从 1 重新数，整段事件全部被丢

所以这里测的是「分配」这一件事的三条性质：单调、并发安全、可从 DB 水位恢复。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from atlas_server.config import get_settings
from atlas_server.redisx import make_redis
from atlas_server.stream.relay import EventRelay, thread_seq_key


@pytest.fixture
async def relay():
    redis = make_redis(get_settings())
    try:
        yield EventRelay(redis), redis
    finally:
        await redis.aclose()


async def test_sequence_is_monotonic(relay) -> None:
    r, redis = relay
    thread_id = uuid4()
    try:
        got = [await r.next_thread_seq(thread_id, floor=0) for _ in range(5)]
        assert got == [1, 2, 3, 4, 5]
    finally:
        await redis.delete(thread_seq_key(thread_id))


async def test_floor_is_only_used_when_the_key_is_missing(relay) -> None:
    """水位是**起点**，不是每次都参考的下限。

    每次都取 max(floor, current) 的话，一个滞后的 floor（DB 还没归档到最新）
    会让序号倒退 —— 而倒退就是撞号。
    """
    r, redis = relay
    thread_id = uuid4()
    try:
        assert await r.next_thread_seq(thread_id, floor=100) == 101
        # 第二次给一个更小的 floor，不该影响已经在走的计数
        assert await r.next_thread_seq(thread_id, floor=0) == 102
    finally:
        await redis.delete(thread_seq_key(thread_id))


async def test_recovery_continues_from_the_db_watermark(relay) -> None:
    """★ 核心回归：Redis 丢了 key 之后，序号必须接着 DB 水位，不能从 1 重来。

    从 1 重来的后果是整段事件落进前端 `seq <= lastSeq` 的丢弃区 —— 界面
    停止更新，而日志里一个字都没有。这是本次改造里最坏的失败形态。
    """
    r, redis = relay
    thread_id = uuid4()
    try:
        assert await r.next_thread_seq(thread_id, floor=0) == 1
        assert await r.next_thread_seq(thread_id, floor=0) == 2

        # 模拟 Redis 重启 / key 被驱逐
        await redis.delete(thread_seq_key(thread_id))

        # 调用方从 run_event 查到的水位是 2（已归档到那里）
        assert await r.next_thread_seq(thread_id, floor=2) == 3, (
            "序号从 1 重来了 —— 前端会把后续事件全部当成重复丢掉"
        )
    finally:
        await redis.delete(thread_seq_key(thread_id))


async def test_concurrent_allocation_never_collides(relay) -> None:
    """★ 并行委派的几个子 run 同时写同一条流 —— 号不能撞。

    这正是「不能在进程内数」的理由：它们是不同的 asyncio.Task，各自的
    EventFactory 互不知情。
    """
    import asyncio

    r, redis = relay
    thread_id = uuid4()
    try:
        got = await asyncio.gather(
            *[r.next_thread_seq(thread_id, floor=0) for _ in range(50)]
        )
        assert sorted(got) == list(range(1, 51)), f"撞号或有空洞：{sorted(got)}"
    finally:
        await redis.delete(thread_seq_key(thread_id))


async def test_the_key_carries_a_long_ttl(relay) -> None:
    """序号是会话的**水位**，不能跟着事件流的 TTL 一起过期。

    与事件流同寿（默认 1 天）的话，一个隔天继续的会话会从 DB 水位恢复 ——
    那条路径是对的，但没必要每天走一次。
    """
    r, redis = relay
    thread_id = uuid4()
    try:
        await r.next_thread_seq(thread_id, floor=0)
        ttl = await redis.ttl(thread_seq_key(thread_id))
        assert ttl > 86_400, f"序号 key 的 TTL 太短：{ttl}s"
    finally:
        await redis.delete(thread_seq_key(thread_id))
