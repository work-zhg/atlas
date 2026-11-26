"""验证 atlas_modelgateway 接入 atlas_redis：AtomicQuota 原子限流与 Fail-Open 容灾。"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from atlas_modelgateway.gateway.admission import QuotaGuard
from atlas_modelgateway.gateway.config import KeyEntry
from atlas_modelgateway.gateway.kernel import GatewayError
from atlas_redis import CircuitBreaker, RedisClient, ResilienceMiddleware
from redis.exceptions import ConnectionError as RedisConnectionError


class _FakeQuotaRaw:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.spends: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def evalsha(self, _sha: str, numkeys: int, *args: Any) -> Any:
        if self.fail:
            raise RedisConnectionError("redis unavailable")
        key = str(args[0])
        rest = args[numkeys:]
        if len(rest) == 1:
            self.counts[key] += 1
            self.ttls[key] = int(rest[0])
            return [self.counts[key], self.ttls[key]]
        self.spends[key] += Decimal(str(rest[0]))
        self.ttls[key] = int(rest[1])
        return str(self.spends[key])

    async def eval(self, _src: str, numkeys: int, *args: Any) -> Any:
        return await self.evalsha("", numkeys, *args)

    async def get(self, key: str) -> Any:
        if self.fail:
            raise RedisConnectionError("redis unavailable")
        val = self.spends.get(key)
        return str(val) if val is not None else None


async def test_quota_guard_uses_atomic_lua_and_fail_open_with_redis_client() -> None:
    raw = _FakeQuotaRaw()
    breaker = CircuitBreaker(service="modelgw", failure_threshold=5)
    client = RedisClient(
        raw,
        service="modelgw",
        middlewares=(ResilienceMiddleware(breaker, retry_backoff_s=0.0),),
    )
    guard = QuotaGuard(client)
    key = KeyEntry(
        key_uuid=uuid4(),
        tenant_uuid=uuid4(),
        kind="client",
        status="active",
        models=frozenset(),
        name="k",
        rpm=2,
        monthly_budget=Decimal("10"),
    )

    await guard.check(key)
    await guard.check(key)
    with pytest.raises(GatewayError):
        await guard.check(key)

    await guard.charge(key.key_uuid, Decimal("2.5"))
    assert await guard.spent(key.key_uuid) == Decimal("2.5")

    # Redis 故障时 Fail-Open 降级，不阻塞推理请求
    raw.fail = True
    await guard.check(key)
    await guard.charge(key.key_uuid, Decimal("1.0"))
    assert await guard.spent(key.key_uuid) == Decimal("3.5")
