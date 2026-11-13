"""设备网关测试的夹具：真的 uvicorn、Redis 15 号库（只清 devicegw: 开头的键）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import redis.asyncio as aioredis
import uvicorn
from atlas_devicegw.app import create_app
from atlas_devicegw.settings import GatewaySettings
from gw_kit import REDIS_URL, Gateway, Upstream, agent_api


@pytest.fixture
async def redis() -> AsyncIterator[aioredis.Redis]:
    r = aioredis.from_url(REDIS_URL)
    await _clear(r)
    yield r
    await _clear(r)
    await r.aclose()


async def _clear(r: aioredis.Redis) -> None:
    keys = [k async for k in r.scan_iter("devicegw:*")]
    if keys:
        await r.delete(*keys)


@pytest.fixture
async def gateway(redis: aioredis.Redis) -> AsyncIterator[Gateway]:
    upstream = Upstream()
    settings = GatewaySettings(
        _env_file=None,
        redis_url=REDIS_URL,
        agent_url="http://agent",
        public_url="https://atlas.example/device-api",
        instance_url="http://gw-1:8095",
        internal_token="internal-secret",
        presence_ttl_s=30,
        stall_s=2.0,
    )
    app = create_app(
        settings,
        redis=redis,
        agent_transport=httpx.MockTransport(agent_api),
        proxy_transport=httpx.MockTransport(upstream),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Gateway(f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}", upstream, settings)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
