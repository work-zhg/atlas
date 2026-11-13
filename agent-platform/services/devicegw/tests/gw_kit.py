"""设备网关测试的辅助：假连接器、假 Agent 服务、假上游、Agent 服务那一侧的连接。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from atlas_devicegw.settings import GatewaySettings
from atlas_devicelink import Channel, Mux
from websockets.asyncio.client import ClientConnection, connect

REDIS_URL = "redis://localhost:6380/15"
DEVICE = "0193f1a0-0000-7000-8000-000000000001"
OWNER = "0193f1a0-0000-7000-8000-0000000000aa"
GOOD_TOKEN = "dev_good"


def agent_api(request: httpx.Request) -> httpx.Response:
    """假 Agent 服务：核对设备令牌、兑换配对码。"""
    body = json.loads(request.content or b"{}")
    identity = {"device_uuid": DEVICE, "owner_uuid": OWNER, "name": "MacBook Pro"}
    if request.url.path == "/internal/devices/authenticate":
        return (
            httpx.Response(200, json=identity)
            if body.get("token") == GOOD_TOKEN
            else httpx.Response(401)
        )
    if request.url.path == "/internal/devices/redeem":
        if body.get("code") == "K7M2-Q9XD":
            return httpx.Response(200, json={**identity, "token": GOOD_TOKEN})
        return httpx.Response(400)
    return httpx.Response(404)


@dataclass
class Upstream:
    """假上游：记下收到的请求，回一段分块的响应。"""

    seen: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)

        async def body() -> AsyncIterator[bytes]:
            for part in (b"data: a\n\n", b"data: b\n\n"):
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


@dataclass
class Gateway:
    url: str
    ws: str
    upstream: Upstream
    settings: GatewaySettings


class WsTransport:
    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def send(self, message: str | bytes) -> None:
        await self.ws.send(message)

    async def receive(self) -> str | bytes | None:
        try:
            return await self.ws.recv()
        except Exception:
            return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.ws.close(code, reason)


OnChannel = Callable[[Channel], Awaitable[None]]


@contextlib.asynccontextmanager
async def fake_connector(
    gw: Gateway, on_channel: OnChannel, *, token: str = GOOD_TOKEN, version: str = "1.0.0"
) -> AsyncIterator[tuple[ClientConnection, Mux, dict[str, Any]]]:
    """连上网关、握手、跑多路复用；网关开的通道交给 on_channel。"""
    ws = await connect(
        gw.ws + "/connect",
        additional_headers={"Authorization": f"Bearer {token}", "X-Connector-Version": version},
        proxy=None,
    )
    await ws.send(
        json.dumps({"t": "hello", "report": {"os": "macOS 15", "clis": {"claude-code": "2.1"}}})
    )
    welcome = json.loads(await ws.recv())
    mux = Mux(WsTransport(ws), opener=False, on_open=on_channel)
    task = asyncio.create_task(mux.run())
    try:
        yield ws, mux, welcome
    finally:
        await mux.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 2)


async def server_side(
    gw: Gateway, thread: str, *, token: str = "internal-secret"
) -> ClientConnection:
    """Agent 服务那一侧：和连 bridge 一样的握手。"""
    return await connect(
        f"{gw.ws}/internal/host/{thread}",
        additional_headers={"Authorization": f"Bearer {token}", "X-Session-Id": thread},
        subprotocols=["atlas.host.v1"],  # type: ignore[list-item]
        proxy=None,
    )
