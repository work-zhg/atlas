"""本机浏览器 · 平台这一头（c4-model/agent/browser/design-browser-device.html）。

Agent 服务的设备宿主（DeviceBrowserHost）、接管台、画面 → 真的设备网关（uvicorn、Redis 15）→ 真的
连接器（本机浏览器组件）。电脑上的守护和画面服务换成替身：守护的命令记下来，画面服务是一个本机的
Unix 套接字。设备归属读 atlas_test 库。

确保只核对（在线、装了组件、本机允许查看画面）· 开关接管窗口经连接器 · 交还不存登录态 ·
画面经画面流原样转发 · 用不了的原因说清楚 · CLI 的 request_takeover（tool.call）在线等。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
import uvicorn
from atlas_agent.browser.device import (
    DeviceBrowserHost,
    DeviceBrowserUnavailable,
    DeviceUnavailable,
)
from atlas_agent.browser.takeover import Takeover, TakeoverDesk
from atlas_agent.browser.view import DeviceView
from atlas_agent.cli.permission import PermissionDesk
from atlas_agent.db.models import Device
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.device.gateway import DeviceGateway
from atlas_connect.audit import AuditLog
from atlas_connect.browser import BrowserDesk
from atlas_connect.config import BrowserSettings, LocalPolicy, Paths
from atlas_connect.credentials import DeviceCredential
from atlas_connect.link import Connector, ReportBuilder
from atlas_devicegw.app import create_app
from atlas_devicegw.settings import GatewaySettings
from atlas_engine.contracts.events import EventType
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import SecretStr

REDIS_URL = "redis://localhost:6380/15"
OWNER = UUID("0193f1a0-0000-7000-8000-0000000000aa")
DEVICE = UUID("0193f1a0-0000-7000-8000-0000000000b1")
ASLEEP = UUID("0193f1a0-0000-7000-8000-0000000000b2")
INTERNAL = "internal-secret"


def agent_api(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    if request.url.path == "/internal/devices/authenticate" and body.get("token") == "dvt_good":
        return httpx.Response(
            200, json={"device_uuid": str(DEVICE), "owner_uuid": str(OWNER), "name": "VPS"}
        )
    return httpx.Response(401)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeLauncher:
    def __init__(self) -> None:
        self.running = False

    async def ensure(self) -> bool:
        was, self.running = self.running, True
        return not was

    async def stop(self) -> bool:
        was, self.running = self.running, False
        return was


class FakeDaemon:
    """电脑上的 atlas-browserd：记下命令；第 1 块属于对话 SCREEN。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.clipboard = ""
        self.slots: dict[str, int] = {}

    async def run(self, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        self.calls.append(args)
        if args[:2] == ("takeover", "open"):
            self.slots[args[-1]] = 1
            return {"target": "T1", "reused": False}
        if args[:2] == ("takeover", "close"):
            return {"closed": True}
        if args[:2] == ("clipboard", "set"):
            self.clipboard = Path(args[2]).read_text(encoding="utf-8")
        if args[:2] == ("clipboard", "get"):
            Path(args[-1]).write_text(self.clipboard, encoding="utf-8")
        return {}

    async def slot_of(self, screen: str) -> int | None:
        return self.slots.get(screen)


@dataclass
class Env:
    root: Path
    desk: BrowserDesk
    daemon: FakeDaemon
    launcher: FakeLauncher
    settings: Any

    def gateway(self) -> DeviceGateway:
        return DeviceGateway(self.settings)

    def thread(self, device: UUID = DEVICE, **kw: Any) -> Any:
        values = {
            "thread_uuid": uuid4(),
            "sandbox_thread_id": uuid4(),
            "created_by_uuid": OWNER,
            "device_uuid": device,
            "owned_by_agent": False,
        }
        return SimpleNamespace(**{**values, **kw})


@contextlib.asynccontextmanager
async def running(**browser: Any) -> AsyncIterator[Env]:
    r = aioredis.from_url(REDIS_URL)
    await _clear(r)
    port = _free_port()
    gw = f"http://127.0.0.1:{port}"
    gw_settings = GatewaySettings(
        _env_file=None,
        redis_url=REDIS_URL,
        agent_url="http://agent",
        public_url="https://atlas.example/device-api",
        instance_url=gw,
        internal_token=INTERNAL,
    )
    app = create_app(gw_settings, redis=r, agent_transport=httpx.MockTransport(agent_api))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    # ★ AF_UNIX 路径不能太长（macOS 104 字节）：连接器的家目录放在 /tmp 下的短路径
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="db-") as tmp:
        root = Path(tmp)
        paths = Paths(root / ".atlas")
        paths.ensure()
        policy = LocalPolicy(browser=BrowserSettings(**browser))
        desk = BrowserDesk(policy, paths, AuditLog(paths.audit))
        desk.probe.missing = lambda: []  # type: ignore[method-assign]
        launcher, daemon = FakeLauncher(), FakeDaemon()
        desk.launcher = desk.reaper.launcher = launcher  # type: ignore[assignment]
        desk._daemon = daemon  # type: ignore[assignment]
        connector = Connector(
            DeviceCredential(gw, str(DEVICE), "VPS", "dvt_good"),
            ReportBuilder(policy, browser=desk.report),
            on_session=lambda ch: ch.close(),
            on_file=lambda ch: ch.close(),
            on_browser=desk.on_browser,
            on_view=desk.on_view,
        )
        run = asyncio.create_task(connector.run())
        for _ in range(200):
            if connector.state == "online":
                break
            await asyncio.sleep(0.02)
        assert connector.state == "online"
        settings = SimpleNamespace(
            device_gateway_url=gw,
            device_gateway_token=SecretStr(INTERNAL),
            device_gateway_timeout_s=5.0,
        )
        try:
            yield Env(root, desk, daemon, launcher, settings)
        finally:
            await connector.stop()
            run.cancel()
            with contextlib.suppress(BaseException):
                await run
            server.should_exit = True
            await asyncio.wait_for(serve, 5)
            await _clear(r)
            await r.aclose()


async def _clear(r: aioredis.Redis) -> None:
    keys = [k async for k in r.scan_iter("devicegw:*")]
    if keys:
        await r.delete(*keys)


@pytest.fixture
async def devices(clean_db: None) -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        session.add(Device(device_uuid=DEVICE, owner_uuid=OWNER, name="VPS", token_hash="c" * 64))
        session.add(
            Device(device_uuid=ASLEEP, owner_uuid=OWNER, name="Office-PC", token_hash="d" * 64)
        )
        await session.commit()
    yield


# ═══════════════════════════════════ 接管窗口、剪贴板、状态 ═══════════════════════════════════


async def test_takeover_window_opens_and_closes_on_the_computer(devices: None) -> None:
    async with running() as env, get_sessionmaker()() as session:
        host = DeviceBrowserHost(session, env.gateway())
        thread = env.thread()
        screen = str(thread.sandbox_thread_id)
        assert await host.running(thread) is False  # 浏览器还没起来：头部不显示「浏览器」
        opened = await host.open_takeover(thread, "a1b2", "https://www.zhipin.com/web/user/")
        assert opened == {"target": "T1", "reused": False}
        assert env.launcher.running  # 浏览器没在跑就由连接器先拉起
        assert await host.running(thread) is True
        await host.clipboard_set(thread, "验证码 6 位")
        assert await host.clipboard_get(thread) == "验证码 6 位"
        await host.close_takeover(thread, "a1b2")
    assert env.daemon.calls[0] == (
        "takeover",
        "open",
        "a1b2",
        "https://www.zhipin.com/web/user/",
        "--screen",
        screen,
    )
    assert ("takeover", "close", "a1b2") in env.daemon.calls  # 只关窗口：没有 --out，不导出


@pytest.mark.parametrize(
    ("browser", "device", "reason"),
    [
        ({"remote_view": False}, DEVICE, DeviceUnavailable.REMOTE_VIEW_OFF),
        ({}, ASLEEP, DeviceUnavailable.OFFLINE),
    ],
)
async def test_unavailable_reasons_are_said_plainly(
    devices: None, browser: dict[str, Any], device: UUID, reason: DeviceUnavailable
) -> None:
    async with running(**browser) as env, get_sessionmaker()() as session:
        host = DeviceBrowserHost(session, env.gateway())
        with pytest.raises(DeviceBrowserUnavailable) as caught:
            await host.open_takeover(env.thread(device), "a1", "https://a.test/")
        assert caught.value.reason is reason
        assert "系统忙" not in str(caught.value)
    assert env.daemon.calls == []


async def test_someone_elses_computer_is_refused(devices: None) -> None:
    async with running() as env, get_sessionmaker()() as session:
        host = DeviceBrowserHost(session, env.gateway())
        with pytest.raises(DeviceBrowserUnavailable) as caught:
            await host.ensure(env.thread(created_by_uuid=uuid4()))
        assert caught.value.reason is DeviceUnavailable.NOT_OWNER


# ═══════════════════════════════════ 交还 ═══════════════════════════════════


class Approvals:
    def __init__(self) -> None:
        self.decided: list[dict[str, Any]] = []

    async def decide_takeover(self, approval_id: UUID, decision: str, **kw: Any) -> None:
        self.decided.append({"id": approval_id, "decision": decision, **kw})


async def test_hand_back_closes_the_window_and_saves_no_login(devices: None) -> None:
    async with running() as env, get_sessionmaker()() as session:
        thread = env.thread()
        approvals = Approvals()
        desk = TakeoverDesk(
            session,
            approvals,  # type: ignore[arg-type]
            None,
            None,
            None,
            device=DeviceBrowserHost(session, env.gateway()),
        )
        approval_id = uuid4()
        takeover = Takeover(
            SimpleNamespace(  # type: ignore[arg-type]
                approval_uuid=approval_id,
                status="pending",
                args={"reason": "请登录", "url": "https://www.zhipin.com/"},
            ),
            SimpleNamespace(run_uuid=uuid4()),  # type: ignore[arg-type]
            thread,
        )

        async def get(_: UUID) -> Takeover:
            return takeover

        desk.get = get  # type: ignore[method-assign]
        await desk.open(OWNER, approval_id)
        result = await desk.hand_back(OWNER, approval_id, "登录好了")
    assert result == {"decision": "approved", "login": None}
    assert approvals.decided == [
        {"id": approval_id, "decision": "approved", "by": OWNER, "note": "登录好了"}
    ]
    assert ("takeover", "close", str(approval_id)) in env.daemon.calls


# ═══════════════════════════════════ 画面 ═══════════════════════════════════


async def test_view_flows_through_the_view_stream(devices: None) -> None:
    async with running() as env:
        thread = env.thread()
        sock = env.desk.env.root / "vnc-1.sock"
        sock.parent.mkdir(parents=True, exist_ok=True)
        env.daemon.slots[str(thread.sandbox_thread_id)] = 1
        env.launcher.running = True

        async def vnc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            data = await reader.read(64)
            writer.write(b"echo:" + data)
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()

        server = await asyncio.start_unix_server(vnc, path=str(sock))
        async with get_sessionmaker()() as session:
            route = await DeviceBrowserHost(session, env.gateway()).view_route(thread)
        assert route.url.endswith(
            f"/internal/view/{DEVICE}?thread={thread.thread_uuid}&screen={thread.sandbox_thread_id}"
        )
        app = FastAPI()

        @app.websocket("/view")
        async def view(ws: WebSocket) -> None:
            subprotocols = list(ws.scope.get("subprotocols") or [])
            await DeviceView().connect(ws, route, subprotocols=subprotocols)

        def browse() -> tuple[str | None, bytes, bytes, tuple[int, str]]:
            with TestClient(app).websocket_connect("/view", subprotocols=["binary"]) as ws:
                hello = ws.receive_bytes()
                ws.send_bytes(b"RFB 003.008\n")
                echo = ws.receive_bytes()
                closed = ws.receive()
                return (
                    ws.accepted_subprotocol,
                    hello,
                    echo,
                    (closed.get("code"), closed.get("reason")),
                )

        try:
            accepted, hello, echo, closed = await asyncio.to_thread(browse)
        finally:
            server.close()
    assert accepted == "binary"
    assert hello == b"RFB 003.008\n" and echo == b"echo:RFB 003.008\n"
    assert closed == (4502, "浏览器画面断开了")


async def test_view_of_a_conversation_that_never_used_the_browser(devices: None) -> None:
    async with running() as env:
        env.launcher.running = True
        thread = env.thread()
        async with get_sessionmaker()() as session:
            route = await DeviceBrowserHost(session, env.gateway()).view_route(thread)
        app = FastAPI()

        @app.websocket("/view")
        async def view(ws: WebSocket) -> None:
            await DeviceView().connect(ws, route)

        def browse() -> dict[str, Any]:
            with TestClient(app).websocket_connect("/view") as ws:
                return ws.receive()

        closed = await asyncio.to_thread(browse)
    assert (closed["code"], closed["reason"]) == (4404, "这个对话还没用过浏览器")


# ═══════════════════════════════════ CLI 的 request_takeover ═══════════════════════════════════


class OnlineApprovals:
    """在线等的审批替身：第一次登记为 pending，wait 时等测试做决定。"""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.decision = asyncio.get_running_loop().create_future()

    async def check(self, **kw: Any) -> str:
        self.registered.append(kw)
        return "pending"

    async def wait(self, approval_id: str, timeout: float) -> str:
        return str(await self.decision)

    async def expire(self, approval_id: str) -> None:
        return None

    async def outcome(self, approval_id: str) -> dict[str, Any]:
        return {"status": await self.decision, "feedback": "登录好了"}


async def test_cli_request_takeover_waits_online_and_returns_the_tool_result() -> None:
    approvals = OnlineApprovals()
    events: list[tuple[EventType, dict[str, Any]]] = []
    run_id = uuid4()
    desk = PermissionDesk(approvals, lambda k, d: events.append((k, d)), run_id=run_id)  # type: ignore[arg-type]
    params = {
        "turnId": "t1",
        "name": "request_takeover",
        "arguments": {"reason": "BOSS 直聘要登录，请扫码", "url": "https://www.zhipin.com/"},
        "callId": "toolu_01",
    }
    call = asyncio.create_task(desk.call_tool(params))
    while not events:
        await asyncio.sleep(0.01)
    kind, data = events[0]
    assert kind is EventType.APPROVAL_REQUIRED
    assert data["approval_kind"] == "takeover" and data["tool_call_id"] == "toolu_01"
    assert data["args"]["url"] == "https://www.zhipin.com/"
    assert approvals.registered[0]["kind"] == "takeover"
    await asyncio.sleep(0.05)
    assert not call.done()  # 一直等，不设期限
    approvals.decision.set_result("approved")
    result = await call
    assert result["isError"] is False and "用户已在浏览器里操作完并交还" in result["content"]
    assert "登录好了" in result["content"]
    settled = events[-1]
    assert settled[0] is EventType.TAKEOVER_SETTLED and settled[1]["decision"] == "approved"
    bad = await desk.call_tool({**params, "arguments": {"url": "file:///etc/passwd"}})
    assert bad["isError"] is True and "参数不合法" in bad["content"]
