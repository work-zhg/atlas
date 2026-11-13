"""本机 CLI · 整条链路（agent-platform/doc/design/device-cli-design.html §3 · §4 · §5）。

Agent 服务的 CliRuntime（设备会话 DeviceSessions 作 CliHost）→ 真的设备网关（uvicorn、Redis 15）
→ 真的连接器 → 真的 bridge 进程 → 假适配器。设备、会话归属读 atlas_test 库。

跑一轮、本机拒绝的原因原样回到网页、电脑不在线、被委派时按规则定目录、能力登记、读本机文件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
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
from atlas_agent.cli.capabilities import CliCapabilities, CliCapabilityAssembly
from atlas_agent.cli.runtime import CliRuntime
from atlas_agent.db.models import Device
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.device.files import DeviceFileError, DeviceFiles, DeviceTarget, FileTooLarge
from atlas_agent.device.gateway import DeviceGateway, SessionGrant
from atlas_agent.device.sessions import CapabilityHandover, DevicePlacement, DeviceSessions
from atlas_agent.sandbox.session_pod import CliModel
from atlas_bridge.testing.fake_agent import Script
from atlas_connect.audit import AuditLog
from atlas_connect.config import CliCommand, LocalPolicy, Paths
from atlas_connect.credentials import DeviceCredential
from atlas_connect.files import FileDesk
from atlas_connect.link import Connector, ReportBuilder
from atlas_connect.session import SessionHost
from atlas_devicegw.app import create_app
from atlas_devicegw.settings import GatewaySettings
from atlas_engine.contracts.events import TraceEvent
from atlas_engine.contracts.spec import CliSpec, KnowledgeSpec
from pydantic import SecretStr

REDIS_URL = "redis://localhost:6380/15"
OWNER = UUID("0193f1a0-0000-7000-8000-0000000000aa")
DEVICE = UUID("0193f1a0-0000-7000-8000-0000000000d1")
#: 配对过、但没连上的另一台电脑
ASLEEP = UUID("0193f1a0-0000-7000-8000-0000000000d2")
INTERNAL = "internal-secret"


def agent_api(request: httpx.Request) -> httpx.Response:
    """设备网关核对设备令牌时问的 Agent 服务（这里只验证链路，登记本身见 test_device_api）。"""
    body = json.loads(request.content or b"{}")
    if request.url.path == "/internal/devices/authenticate" and body.get("token") == "dvt_good":
        return httpx.Response(
            200, json={"device_uuid": str(DEVICE), "owner_uuid": str(OWNER), "name": "MacBook"}
        )
    return httpx.Response(401)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _clear(r: aioredis.Redis) -> None:
    keys = [k async for k in r.scan_iter("devicegw:*")]
    if keys:
        await r.delete(*keys)


class Approvals:
    async def check(self, **_: Any) -> str:
        return "approved"

    async def wait(self, approval_id: str, timeout: float) -> str:
        return "approved"

    async def expire(self, approval_id: str) -> None:
        return None


class Sessions:
    async def remember(self, thread_id: UUID, agent_session_id: str) -> None:
        return None

    async def save_progress(self, thread_id: UUID, *, instance: str, acked_seq: int) -> None:
        return None


class Relay:
    async def is_cancelled(self, run_id: UUID) -> bool:
        return False


@dataclass
class Env:
    root: Path
    paths: Paths
    host: SessionHost
    settings: Any
    redis: aioredis.Redis
    runtime: CliRuntime

    def thread(self, **kw: Any) -> Any:
        values = {
            "thread_uuid": uuid4(),
            "created_by_uuid": OWNER,
            "root_thread_uuid": None,
            "device_uuid": DEVICE,
            "device_project_dir": str(self.root / "p"),
            "source_app": None,
            "credential_profiles": [],
            "agent_uuid": uuid4(),
        }
        return SimpleNamespace(**{**values, **kw})

    async def run(self, thread: Any, cli: CliSpec) -> list[TraceEvent]:
        spec = SimpleNamespace(
            slug="local",
            name="本机 Claude Code",
            cli=cli,
            tool_names=(),
            skills=(),
            limits=SimpleNamespace(timeout_s=60),
            knowledge=KnowledgeSpec(),
            model=SimpleNamespace(model="m1", prompt_cache=False),
        )
        segment = SimpleNamespace(
            spec=spec,
            input_content="hi",
            thread=thread,
            thread_id=thread.thread_uuid,
            start_seq=0,
            base_depth=0,
            takeover=None,
            cli_session_id=None,
            cli_bridge_instance=None,
            cli_acked_seq=0,
            approval_mode=None,
        )
        out: list[TraceEvent] = []
        gen = self.runtime.run_turn(
            segment,  # type: ignore[arg-type]
            run_id=uuid4(),
            redis=None,  # type: ignore[arg-type]
            relay=Relay(),  # type: ignore[arg-type]
        )
        async for event in gen:
            out.append(event)
        return out


def cli_spec(root: Path, **kw: Any) -> CliSpec:
    values: dict[str, Any] = {
        "cli_type": "claude-code",
        "placement": "device",
        "default_project_dir": str(root),
        "model_source": "local_login",
    }
    return CliSpec(**{**values, **kw})


@contextlib.asynccontextmanager
async def running(tmp_path: Path, **policy_kw: Any) -> AsyncIterator[Env]:
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

    root = tmp_path / "projects"
    root.mkdir()
    paths = Paths(tmp_path / ".atlas")
    paths.ensure()
    fake = (sys.executable, "-m", "atlas_bridge.testing.fake_agent", Script().to_json())
    policy = LocalPolicy(
        allowed_roots=(str(root),), clis={"claude-code": CliCommand(fake)}, **policy_kw
    )
    audit = AuditLog(paths.audit)
    host = SessionHost(policy, paths, audit)
    connector = Connector(
        DeviceCredential(gw, str(DEVICE), "MacBook", "dvt_good"),
        ReportBuilder(policy),
        on_session=host.on_session,
        on_file=FileDesk(policy, audit).on_file,
    )
    run = asyncio.create_task(connector.run())
    for _ in range(200):
        if connector.state == "online":
            break
        await asyncio.sleep(0.02)
    assert connector.state == "online"

    settings = SimpleNamespace(
        cli_ws_connect_timeout_s=5.0,
        cli_prompt_idle_timeout_s=120.0,
        cli_tool_idle_timeout_s=600.0,
        model_platform_url=None,
        credential_profiles={},
        device_gateway_url=gw,
        device_gateway_token=SecretStr(INTERNAL),
        device_gateway_timeout_s=5.0,
    )
    runtime = CliRuntime(
        get_sessionmaker(),
        settings,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]  会话 Pod 用不到
        devices=DeviceSessions(get_sessionmaker(), DeviceGateway(settings)),  # type: ignore[arg-type]
        approvals=lambda run_id, redis: Approvals(),  # type: ignore[arg-type,return-value]
        sessions=Sessions(),
    )
    try:
        yield Env(root, paths, host, settings, r, runtime)
    finally:
        await connector.stop()
        run.cancel()
        with contextlib.suppress(BaseException):
            await run
        await host.shutdown()
        server.should_exit = True
        await asyncio.wait_for(serve, 5)
        await _clear(r)
        await r.aclose()


@pytest.fixture
async def devices(clean_db: None) -> AsyncIterator[None]:
    async with get_sessionmaker()() as session:
        session.add(
            Device(device_uuid=DEVICE, owner_uuid=OWNER, name="MacBook", token_hash="a" * 64)
        )
        session.add(
            Device(
                device_uuid=ASLEEP,
                owner_uuid=OWNER,
                name="Office-PC",
                token_hash="b" * 64,
                is_default=True,
            )
        )
        await session.commit()
    yield


def kinds(events: list[TraceEvent]) -> list[str]:
    return [e.type.value for e in events]


# ═══════════════════════════════════ 一轮 ═══════════════════════════════════


async def test_a_turn_runs_on_the_users_computer(
    tmp_path: Path, devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def platform_model(self: Any, segment: Any) -> CliModel:
        return CliModel("http://litellm:4000", "sk-agent", "m1", "m1-small", {}, False)

    monkeypatch.setattr(CliCapabilityAssembly, "_model", platform_model)
    async with running(tmp_path) as env:
        thread = env.thread()
        events = await env.run(thread, cli_spec(env.root, model_source="platform"))
        assert kinds(events)[0] == "run.started" and kinds(events)[-1] == "run.finished", events
        assert (env.root / "p").is_dir()  # 目录由连接器建

        # 能力登记：真实凭据只在设备网关，CLI 拿到的是网关地址和会话令牌
        binding = json.loads(await env.redis.get(f"devicegw:session:{thread.thread_uuid}"))
        assert binding["upstreams"]["model"] == {
            "kind": "model",
            "url": "http://litellm:4000",
            "headers": {"Authorization": "Bearer sk-agent"},
        }
        assert binding["launch"] == {
            "cli_type": "claude-code",
            "project_dir": str(env.root / "p"),
            "mode": "auto",
            "create_dir": True,
            "model": {
                "upstream": "model",
                "model": "m1",
                "small_model": "m1-small",
                "prompt_cache": False,
            },
        }
        assert binding["owner"] == str(OWNER) and binding["device"] == str(DEVICE)


async def test_local_rejection_reaches_the_user(tmp_path: Path, devices: None) -> None:
    async with running(tmp_path) as env:
        outside = tmp_path / "secret"
        outside.mkdir()
        events = await asyncio.wait_for(
            env.run(env.thread(device_project_dir=str(outside)), cli_spec(env.root)), 30
        )
        failed = events[-1]
        assert failed.type.value == "run.failed"
        assert failed.data["error_kind"] == "device_rejected"
        assert "不在这台电脑允许的范围内" in failed.data["message"]
        assert env.host.bridges.running == []


async def test_mode_above_the_local_cap_is_refused(tmp_path: Path, devices: None) -> None:
    async with running(tmp_path, max_mode="acceptEdits") as env:
        events = await asyncio.wait_for(
            env.run(env.thread(), cli_spec(env.root, permission_mode="auto")), 30
        )
        assert events[-1].data["error_kind"] == "device_rejected"
        assert "超过了这台电脑允许的上限" in events[-1].data["message"]


async def test_offline_or_revoked_computer_fails_clearly(tmp_path: Path, devices: None) -> None:
    async with running(tmp_path) as env:
        events = await env.run(env.thread(device_uuid=ASLEEP), cli_spec(env.root))
        assert events[-1].data["error_kind"] == "device_offline"
        assert "「Office-PC」不在线" in events[-1].data["message"]

        async with get_sessionmaker()() as session:
            device = await session.get(Device, (await _id_of(session, DEVICE)))
            assert device is not None
            device.status = "revoked"
            await session.commit()
        events = await env.run(env.thread(), cli_spec(env.root))
        assert events[-1].data["error_kind"] == "device_revoked"


async def _id_of(session: Any, device: UUID) -> int:
    from sqlalchemy import select

    return int(await session.scalar(select(Device.id).where(Device.device_uuid == device)))


async def test_delegated_thread_uses_default_device_and_root_dir(
    tmp_path: Path, devices: None
) -> None:
    async with get_sessionmaker()() as session:  # 默认设备换成在线的那台
        from sqlalchemy import update

        await session.execute(update(Device).values(is_default=False))
        await session.execute(
            update(Device).where(Device.device_uuid == DEVICE).values(is_default=True)
        )
        await session.commit()
    async with running(tmp_path) as env:
        root_thread = uuid4()
        thread = env.thread(device_uuid=None, device_project_dir=None, root_thread_uuid=root_thread)
        events = await env.run(thread, cli_spec(env.root))
        assert kinds(events)[-1] == "run.finished", events
        assert thread.device_uuid == DEVICE
        assert thread.device_project_dir == f"{env.root}/{root_thread}"
        assert (env.root / str(root_thread)).is_dir()


# ═══════════════════════════════════ 能力交接 ═══════════════════════════════════


async def test_mcp_servers_point_at_the_gateway_with_the_session_token() -> None:
    registered: dict[str, Any] = {}

    class Gateway:
        async def register(self, thread: str, **kw: Any) -> SessionGrant:
            registered.update(kw)
            ids = [u.id for u in kw["upstreams"]]
            return SessionGrant(
                "dst_x", {i: f"https://atlas.example/device-api/u/{i}" for i in ids}
            )

        def host_url(self, status: Any, thread: str) -> tuple[str, str]:
            return f"ws://gw/internal/host/{thread}", INTERNAL

    caps = CliCapabilities(
        mcp_servers=[
            {
                "type": "http",
                "name": "knowledge",
                "url": "http://kb:8050/mcp/",
                "headers": [{"name": "X-Knowledge-Token", "value": "signed"}],
            }
        ],
        permission_mode="manual",
    )
    thread = SimpleNamespace(thread_uuid=uuid4(), created_by_uuid=OWNER)
    access = await CapabilityHandover(Gateway()).hand_over(  # type: ignore[arg-type]
        thread,  # type: ignore[arg-type]
        DevicePlacement(DEVICE, "~/atlas-projects/x"),
        cli_spec(Path("/tmp")),
        caps,
        SimpleNamespace(instance="http://gw"),  # type: ignore[arg-type]
    )
    (upstream,) = registered["upstreams"]
    assert upstream.url == "http://kb:8050/mcp/" and upstream.headers == {
        "X-Knowledge-Token": "signed"
    }
    (server,) = access.caps.mcp_servers
    assert server["url"] == f"https://atlas.example/device-api/u/{upstream.id}"
    assert server["headers"] == [{"name": "Authorization", "value": "Bearer dst_x"}]
    assert registered["launch"]["mode"] == "manual" and "model" not in registered["launch"]
    assert caps.mcp_servers[0]["url"] == "http://kb:8050/mcp/"  # 平台这边的原样不动


# ═══════════════════════════════════ 本机文件 ═══════════════════════════════════


async def test_files_are_read_from_the_computer(tmp_path: Path, devices: None) -> None:
    async with running(tmp_path) as env:
        project = env.root / "shop"
        (project / "src").mkdir(parents=True)
        (project / "src" / "app.ts").write_text("export const a = 1;\n")
        (project / ".env").write_text("SECRET=1")
        files = DeviceFiles(DeviceGateway(env.settings))
        target = DeviceTarget(OWNER, DEVICE, str(project), "MacBook")

        listed = await files.list_dir(target, "")
        assert [e["name"] for e in listed["entries"]] == ["src"]  # 以点开头的不列
        changes = await files.changes(target)
        assert changes == {"is_repo": False, "entries": []}

        fetched = await files.fetch(target, "src/app.ts", limit_mb=20)
        body = b"".join([chunk async for chunk in fetched.body or _empty()])
        assert fetched.status == 200 and body == b"export const a = 1;\n"
        etag = fetched.headers["etag"]
        again = await files.fetch(target, "src/app.ts", limit_mb=20, if_none_match=etag)
        assert again.status == 304 and again.body is None

        with pytest.raises(FileTooLarge):
            await files.fetch(target, "src/app.ts", limit_mb=0)
        with pytest.raises(DeviceFileError) as denied:
            await files.fetch(target, ".env", limit_mb=20)
        assert denied.value.status_code == 403
        with pytest.raises(DeviceFileError) as stranger:
            await files.list_dir(DeviceTarget(uuid4(), DEVICE, str(project)), "")
        assert stranger.value.kind == "device_forbidden"
        with pytest.raises(DeviceFileError) as offline:
            await files.list_dir(DeviceTarget(OWNER, ASLEEP, str(project), "Office-PC"), "")
        assert offline.value.kind == "device_offline"


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""
