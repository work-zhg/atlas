"""RedisRuntime 装配、舱壁池隔离、DistributedLock 集成与生命周期单测。"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_redis import CommandForbidden, RedisConfig, RedisRuntime


class _LockFakeRaw:
    """支持 RedisLock（SET NX PX + Lua 续租/释放）与 PING 的内存假客户端。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    async def eval(self, script: str, _numkeys: int, key: str, owner: str, *_rest: Any) -> int:
        if "del" in script:
            if self.kv.get(key) == owner:
                del self.kv[key]
                return 1
            return 0
        if "pexpire" in script:
            return 1 if self.kv.get(key) == owner else 0
        return 0

    async def aclose(self) -> None:
        self.closed = True


async def test_runtime_wires_bulkhead_pools_and_distributed_lock() -> None:
    cfg = RedisConfig(
        url="redis://localhost:6380/15",
        service="agent",
        command_socket_timeout_s=1.5,
        command_max_connections=64,
        blocking_max_connections=128,
    )
    raw_cmd = _LockFakeRaw()
    raw_blk = _LockFakeRaw()
    rt = RedisRuntime(
        cfg,
        enable_otel=False,
        _commands_raw=raw_cmd,
        _blocking_raw=raw_blk,
        _pubsub_factory=lambda: None,
    )

    async with rt.lifespan():
        assert await rt.ping() is True

        # 分布式锁复用 atlas_redislock，同时天然经过 commands 拦截链与遥测统计
        lock_key = rt.keyspace("lock").key("doc-1")
        lock = rt.lock(lock_key, ttl_s=10.0)
        async with lock.hold():
            assert raw_cmd.kv[lock_key] == lock.owner
        assert lock_key not in raw_cmd.kv

        # 护栏在 Runtime 上默认生效
        with pytest.raises(CommandForbidden):
            await rt.commands.execute("KEYS", "*")

        stats = rt.pool_stats()
        assert stats["service"] == "agent"
        assert stats["breaker_state"] == "closed"
        assert stats["telemetry"]["total_commands"] >= 3


def test_real_pool_timeouts_are_physically_isolated() -> None:
    """验证真实构造的两个 BlockingConnectionPool 超时与容量参数严格隔离。"""
    cfg = RedisConfig(
        url="redis://127.0.0.1:6380/15",
        service="modelgw",
        command_socket_timeout_s=2.0,
        command_max_connections=50,
        blocking_max_connections=120,
    )
    rt = RedisRuntime(cfg, enable_otel=False)
    cmd_pool = rt.commands.raw.connection_pool
    blk_pool = rt.blocking.raw.connection_pool

    assert cmd_pool is not blk_pool
    assert cmd_pool.max_connections == 50
    assert blk_pool.max_connections == 120
    assert cmd_pool.connection_kwargs["socket_timeout"] == 2.0
    assert blk_pool.connection_kwargs["socket_timeout"] is None
