"""浏览器 · 画面（c4-model/agent/browser/code-browser-view.html）。

转发      两边原样转发二进制帧；客户端要的子协议交给画面服务，接受它选的那个；带上画面口令
关闭      画面那边断了、沙箱不在了、画面服务连不上：带着原因关掉客户端
保活      连着画面时给沙箱记活动
核对      只给会话的创建人；浏览器没在运行不给看（不为看画面建沙箱）
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_agent.browser.daemon import ViewKey
from atlas_agent.browser.view import (
    CloseReason,
    LiveView,
    ViewAccess,
    ViewDenied,
    ViewKeepAlive,
    ViewTarget,
)
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.ids import uuid7
from atlas_agent.main import create_app
from atlas_agent.sandbox.base.execution.channels import ChannelUnavailable
from atlas_agent.sandbox.base.lifecycle.runtime import SandboxGone
from atlas_agent.sandbox.provisioning.model import SandboxRef
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from tests.fake_uc import AS_USER, TEST_USER

TARGET = ViewTarget(uuid7(), 6080, {"Authorization": "Basic abc"})


class Upstream:
    """画面服务的替身：把收到的帧加上前缀回过去；close_after 帧之后自己断开。"""

    subprotocol = "binary"

    def __init__(self, close_after: int | None = None) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._left = close_after

    async def send(self, data: bytes | str) -> None:
        payload = data if isinstance(data, bytes) else data.encode()
        await self._queue.put(b"echo:" + payload)
        if self._left is not None:
            self._left -= 1
            if self._left <= 0:
                await self._queue.put(None)

    def __aiter__(self) -> Upstream:
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class Channels:
    def __init__(self, upstream: Upstream | None = None, error: Exception | None = None) -> None:
        self.upstream = upstream or Upstream()
        self.error = error
        self.opened: list[dict[str, Any]] = []
        self.kept: list[UUID] = []

    @asynccontextmanager
    async def open_ws(self, sandbox_uuid: UUID, port: int, **kw: Any) -> AsyncIterator[Upstream]:
        self.opened.append({"sandbox": sandbox_uuid, "port": port, **kw})
        if self.error is not None:
            raise self.error
        yield self.upstream

    async def keep_alive(self, sandbox_uuid: UUID) -> None:
        self.kept.append(sandbox_uuid)


def _client(channels: Channels) -> TestClient:
    app = FastAPI()

    @app.websocket("/view")
    async def view(ws: WebSocket) -> None:
        live = LiveView(
            None,  # type: ignore[arg-type]  # 核对另测
            channels,  # type: ignore[arg-type]
            keep_alive=ViewKeepAlive(channels, interval_s=0.01),  # type: ignore[arg-type]
        )
        await live.connect(ws, TARGET, subprotocols=list(ws.scope.get("subprotocols") or []))

    return TestClient(app)


def test_frames_are_relayed_both_ways_with_the_view_key() -> None:
    channels = Channels()
    with _client(channels).websocket_connect("/view", subprotocols=["binary"]) as ws:
        assert ws.accepted_subprotocol == "binary"
        ws.send_bytes(b"\x00\x01key")
        assert ws.receive_bytes() == b"echo:\x00\x01key"
        ws.send_bytes(b"more")
        assert ws.receive_bytes() == b"echo:more"
    [opened] = channels.opened
    assert opened["sandbox"] == TARGET.sandbox_uuid and opened["port"] == 6080
    assert opened["path"] == "/websockify"
    assert opened["headers"] == {"Authorization": "Basic abc"}
    assert opened["subprotocols"] == ["binary"]
    assert TARGET.sandbox_uuid in channels.kept


def test_upstream_closing_closes_the_client_with_a_reason() -> None:
    channels = Channels(Upstream(close_after=1))
    with _client(channels).websocket_connect("/view") as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"echo:x"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_bytes()
    assert closed.value.code == CloseReason.UPSTREAM_CLOSED.code


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SandboxGone("没了"), CloseReason.SANDBOX_GONE),
        (ChannelUnavailable("连不上"), CloseReason.NO_BROWSER),
    ],
)
def test_upstream_failures_close_with_a_reason(error: Exception, reason: CloseReason) -> None:
    with (
        _client(Channels(error=error)).websocket_connect("/view") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_bytes()
    assert closed.value.code == reason.code
    assert closed.value.reason == reason.text


class Hosts:
    def __init__(self, ref: SandboxRef | None) -> None:
        self.ref = ref

    async def current(self, thread: Any) -> SandboxRef | None:
        return self.ref


@pytest.mark.usefixtures("clean_db")
async def test_access_only_for_the_creator_with_a_running_browser() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test", headers=AS_USER
    ) as client:
        spec = {"model": {"model": "claude-opus-5"}}
        body = {"slug": "viewer", "name": "画面", "spec": spec}
        agent = await client.post("/v1/agents", json=body)
        thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    thread_id = UUID(thread.json()["id"])
    keys = ViewKey(SecretStr("s"))
    ref = SandboxRef(uuid7())
    async with get_sessionmaker()() as session:
        target = await ViewAccess(Hosts(ref), keys, 6080).check(  # type: ignore[arg-type]
            session, UUID(TEST_USER), thread_id
        )
        assert target == ViewTarget(ref.sandbox_uuid, 6080, keys.auth_header(ref.sandbox_uuid))
        with pytest.raises(ViewDenied) as denied:
            await ViewAccess(Hosts(ref), keys, 6080).check(session, uuid7(), thread_id)  # type: ignore[arg-type]
        assert denied.value.reason is CloseReason.NOT_OWNER
        with pytest.raises(ViewDenied) as denied:
            await ViewAccess(Hosts(None), keys, 6080).check(  # type: ignore[arg-type]
                session, UUID(TEST_USER), thread_id
            )
        assert denied.value.reason is CloseReason.NO_BROWSER
