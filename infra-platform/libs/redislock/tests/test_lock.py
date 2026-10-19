"""分布式锁的行为测试。用真 Redis：锁的语义全在 Lua 与过期时间里，假实现测不出来。

REDIS_URL 默认是 docker-compose 的本地端口；每个用例一个随机键，结束即删，不碰别的数据。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from atlas_redislock import LockBusy, LockLost, RedisLock

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
async def redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def key(redis: aioredis.Redis) -> AsyncIterator[str]:
    name = f"atlas-redislock-test:{uuid4().hex}"
    try:
        yield name
    finally:
        await redis.delete(name)


# ---------------------------------------------------------------------------
# 原语
# ---------------------------------------------------------------------------


async def test_acquire_is_exclusive(redis: aioredis.Redis, key: str) -> None:
    a, b = RedisLock(redis, key), RedisLock(redis, key)
    assert await a.acquire()
    assert not await b.acquire()
    assert await a.release()
    assert await b.acquire()


async def test_release_requires_owner(redis: aioredis.Redis, key: str) -> None:
    """★ TTL 过期后被别人拿到的锁，旧持有者收尾时不得误删。"""
    old, new = RedisLock(redis, key), RedisLock(redis, key)
    assert await old.acquire()
    await redis.delete(key)  # 模拟 TTL 过期
    assert await new.acquire()

    assert not await old.release()
    assert await new.owned()


async def test_renew_requires_owner(redis: aioredis.Redis, key: str) -> None:
    old, new = RedisLock(redis, key, ttl_s=5), RedisLock(redis, key, ttl_s=5)
    assert await old.acquire()
    assert await old.renew()

    await redis.delete(key)
    assert not await old.renew()  # 过期了就不再续，不会偷偷重新占上
    assert await new.acquire()
    assert not await old.renew()


async def test_reclaim_takes_back_expired_but_not_others(redis: aioredis.Redis, key: str) -> None:
    """接管：同一个 owner 在锁过期后能重新占上，锁在别人手里时不行。"""
    run_id = uuid4().hex
    assert await RedisLock(redis, key, owner=run_id).acquire()
    await redis.delete(key)

    successor = RedisLock(redis, key, owner=run_id)  # 另一个实例接管同一个 run
    assert await successor.reclaim()
    assert await successor.owned()

    await redis.set(key, "someone-else")
    assert not await successor.reclaim()
    assert await redis.get(key) == "someone-else"


async def test_acquire_waits_until_released(redis: aioredis.Redis, key: str) -> None:
    holder, waiter = RedisLock(redis, key), RedisLock(redis, key)
    assert await holder.acquire()
    assert not await waiter.acquire(wait_s=0.2)

    async def release_soon() -> None:
        await asyncio.sleep(0.2)
        await holder.release()

    releaser = asyncio.create_task(release_soon())
    assert await waiter.acquire(wait_s=5)
    await releaser


async def test_works_without_decode_responses(key: str) -> None:
    client = aioredis.from_url(REDIS_URL)
    try:
        lock = RedisLock(client, key)
        assert await lock.acquire()
        assert await lock.owned()
        assert await lock.renew()
        assert await lock.release()
    finally:
        await client.delete(key)
        await client.aclose()


# ---------------------------------------------------------------------------
# hold()：看门狗
# ---------------------------------------------------------------------------


async def test_hold_outlives_ttl_and_releases(redis: aioredis.Redis, key: str) -> None:
    """TTL 不是工作时长上限：body 跑了好几个 TTL，锁一直在自己手里。"""
    lock = RedisLock(redis, key, ttl_s=0.3)
    async with lock.hold():
        await asyncio.sleep(1.0)
        assert await lock.owned()
    assert not await redis.exists(key)


async def test_hold_raises_busy(redis: aioredis.Redis, key: str) -> None:
    assert await RedisLock(redis, key).acquire()
    with pytest.raises(LockBusy):
        async with RedisLock(redis, key).hold():
            pytest.fail("不该进入 body")


async def test_hold_cancels_body_when_lock_is_taken(redis: aioredis.Redis, key: str) -> None:
    lock = RedisLock(redis, key, ttl_s=0.3)
    reached_end = False
    with pytest.raises(LockLost):
        async with lock.hold():
            await redis.set(key, "someone-else")  # 锁被别人拿走
            await asyncio.sleep(5)
            reached_end = True
    assert not reached_end
    assert await redis.get(key) == "someone-else"  # 不是自己的，不删


async def test_hold_lost_when_redis_hangs(redis: aioredis.Redis, key: str) -> None:
    """续租请求一直挂着（断网、无 socket_timeout）：有效期一过就判定失锁，不跟着挂。"""

    class HangingEval:
        def __init__(self, inner: aioredis.Redis) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def eval(self, *args: Any) -> Any:
            await asyncio.Event().wait()

    lock = RedisLock(HangingEval(redis), key, ttl_s=0.3)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(LockLost):
        async with lock.hold():
            await asyncio.sleep(5)
    assert loop.time() - started < 1.0


async def test_hold_passes_external_cancel_through(redis: aioredis.Redis, key: str) -> None:
    """外部取消不是失锁：照常抛 CancelledError，并且放锁。"""
    entered = asyncio.Event()

    async def worker() -> None:
        async with RedisLock(redis, key, ttl_s=0.3).hold():
            entered.set()
            await asyncio.sleep(5)

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await redis.exists(key)


async def test_hold_does_not_hit_await_after_body_finished(redis: aioredis.Redis, key: str) -> None:
    """失锁的取消还没送达 body 就结束了：出口处收回这次取消，调用方的下一个 await 不受影响。"""
    lock = RedisLock(redis, key, ttl_s=0.3)
    hold = lock.hold()
    await hold.__aenter__()
    hold._lose()  # 看门狗刚判定失锁，body 恰好在没有 await 的地方结束
    await hold.__aexit__(None, None, None)
    await asyncio.sleep(0)  # 不应抛 CancelledError
