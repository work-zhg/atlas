"""几个网关实例共用网关状态（Redis）：一个实例撞上限流，别的实例马上知道；花费、配置版本号共用。

用本机 Redis 的 11 号库（别的测试没用它），每个测试前后清空。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as aioredis
from atlas_modelgateway.gateway.app import build_runtime, create_gateway_app
from atlas_modelgateway.gateway.config import VersionWatch
from atlas_modelgateway.gateway.format import AddressGuard
from atlas_modelgateway.settings import get_settings
from gw_testkit import TENANT_A, FakeUpstream, RouteSpec, anthropic_json, error, seed

REDIS_URL = "redis://localhost:6380/11"


@pytest.fixture
async def redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        pytest.skip("本机没有 Redis（localhost:6380）")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def _instance(redis: aioredis.Redis, upstream: FakeUpstream):
    settings = get_settings().model_copy(update={"config_check_interval_s": 0.0})
    runtime = build_runtime(
        settings, redis=redis, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    return runtime, create_gateway_app(settings, runtime=runtime)


async def test_cooling_and_spend_are_shared(redis: aioredis.Redis) -> None:
    upstream = FakeUpstream()
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test", priority=1), RouteSpec("b.test", priority=2)],
        price={"input": "1000000"},
        budget=Decimal("100"),
    )
    upstream.on(
        "a.test",
        error(429, {"type": "error", "error": {"type": "rate_limit_error"}}, {"retry-after": "30"}),
    )
    upstream.on("b.test", anthropic_json(usage_in=3, usage_out=0))
    rt1, app1 = await _instance(redis, upstream)
    rt2, app2 = await _instance(redis, upstream)
    async with (
        app1.router.lifespan_context(app1),
        app2.router.lifespan_context(app2),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://gw1") as c1,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://gw2") as c2,
    ):
        body = {"model": "m1", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
        assert (
            await c1.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
        ).status_code == 200
        assert len(upstream.hits("a.test")) == 1
        # 实例 2 没撞过 429，但知道线路 1 在冷却
        assert (
            await c2.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
        ).status_code == 200
        assert len(upstream.hits("a.test")) == 1
        ttl = await redis.ttl(
            f"modelgw:cool:{next(iter(rt1.config._tenants[TENANT_A].value.models['m1'].routes)).uuid}"
        )
        assert 25 <= ttl <= 30
        import asyncio

        await asyncio.sleep(0.05)
        spent = await rt2.admission.quota.spent(s.key_uuid)
        assert spent == Decimal("6")  # 两次各 3 token × 100 万/百万
    for rt in (rt1, rt2):
        await rt.upstream.aclose()


async def test_config_version_bump_reloads(redis: aioredis.Redis) -> None:
    upstream = FakeUpstream()
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")], key_models=["m1"])
    upstream.on("a.test", anthropic_json())
    rt, app = await _instance(redis, upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        body = {"model": "m1", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
        assert (
            await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
        ).status_code == 200
        # 管理平台停用这把 Key：改库 + 版本号加一
        from atlas_modelgateway.db.models import GwApiKey
        from atlas_modelgateway.db.session import get_sessionmaker
        from atlas_tenancy import TenantScope
        from sqlalchemy import update

        with TenantScope.enter(TENANT_A):
            async with get_sessionmaker()() as session:
                await session.execute(update(GwApiKey).values(status="blocked"))
                await session.commit()
        await VersionWatch.bump(redis)
        # 后台重新加载：第一次可能还用旧的，很快就拦住
        import asyncio

        for _ in range(20):
            r = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
            if r.status_code == 401:
                break
            await asyncio.sleep(0.05)
        assert r.status_code == 401
    await rt.upstream.aclose()
