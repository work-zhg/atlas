"""真实链路：设备网关 + 连接器 + 真的 bridge 进程 + 假适配器。

Agent 服务那一侧用 UpstreamProbe 扮演。

跑一轮、断线后 attach 补发、本机拒绝（目录、权限模式）、经网关读文件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import redis.asyncio as aioredis
import uvicorn
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.probe import UpstreamProbe
from atlas_connect.audit import AuditLog
from atlas_connect.config import CliCommand, LocalPolicy, Paths
from atlas_connect.credentials import DeviceCredential
from atlas_connect.files import FileDesk
from atlas_connect.link import Connector, ReportBuilder
from atlas_connect.session import SessionHost
from atlas_devicegw.app import create_app
from atlas_devicegw.settings import GatewaySettings
from cn_kit import fake_agent

REDIS_URL = "redis://localhost:6380/15"
DEVICE = "0193f1a0-0000-7000-8000-0000000000d1"
OWNER = "0193f1a0-0000-7000-8000-0000000000aa"
THREAD = "0193f1a0-0000-7000-8000-000000000020"
INTERNAL = "internal-secret"
PROMPT = [{"type": "text", "text": "hi"}]


def agent_api(request: httpx.Request) -> httpx.Response:
    identity = {"device_uuid": DEVICE, "owner_uuid": OWNER, "name": "MacBook Pro"}
    body = json.loads(request.content or b"{}")
    if request.url.path == "/internal/devices/authenticate" and body.get("token") == "dev_good":
        return httpx.Response(200, json=identity)
    return httpx.Response(401)


@dataclass
class Env:
    gw: str
    ws: str
    root: Path
    paths: Paths
    host: SessionHost

    async def register(self, *, project: str, mode: str = "auto", create: bool = True) -> None:
        async with httpx.AsyncClient(base_url=self.gw, trust_env=False) as client:
            resp = await client.put(
                f"/internal/sessions/{THREAD}",
                json={
                    "device": DEVICE,
                    "owner": OWNER,
                    "launch": {
                        "cli_type": "claude-code",
                        "project_dir": project,
                        "mode": mode,
                        "create_dir": create,
                    },
                    "upstreams": [],
                },
            )
            resp.raise_for_status()

    async def probe(self) -> UpstreamProbe:
        return await UpstreamProbe.connect(
            f"{self.ws}/internal/host/{THREAD}", token=INTERNAL, session_id=THREAD
        )


async def _clear(r: aioredis.Redis) -> None:
    keys = [k async for k in r.scan_iter("devicegw:*")]
    if keys:
        await r.delete(*keys)


@contextlib.asynccontextmanager
async def running(tmp_path: Path, script: Script | None = None, **policy_kw) -> AsyncIterator[Env]:
    r = aioredis.from_url(REDIS_URL)
    await _clear(r)
    settings = GatewaySettings(
        _env_file=None,
        redis_url=REDIS_URL,
        agent_url="http://agent",
        public_url="http://127.0.0.1/device-api",
        instance_url="http://gw-1",
        internal_token=INTERNAL,
    )
    app = create_app(settings, redis=r, agent_transport=httpx.MockTransport(agent_api))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    gw = f"http://127.0.0.1:{port}"

    root = tmp_path / "projects"
    root.mkdir()
    paths = Paths(tmp_path / ".atlas")
    paths.ensure()
    policy = LocalPolicy(
        allowed_roots=(str(root),),
        clis={"claude-code": CliCommand(fake_agent(script))},
        **policy_kw,
    )
    audit = AuditLog(paths.audit)
    host = SessionHost(policy, paths, audit)
    connector = Connector(
        DeviceCredential(gw, DEVICE, "MacBook Pro", "dev_good"),
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
    try:
        yield Env(gw, f"ws://127.0.0.1:{port}", root, paths, host)
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


async def test_a_turn_runs_on_the_computer(tmp_path: Path) -> None:
    async with running(tmp_path) as env:
        project = env.root / "3f2a"
        await env.register(project=str(project))
        probe = await env.probe()
        opened = await probe.request("session.open", {})
        assert opened["resumed"] is False
        assert await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT}) == {
            "accepted": True
        }
        ended = await probe.turn_ended("run-1", timeout=15)
        assert ended["outcome"]["kind"] == "completed"
        assert project.is_dir()  # 默认目录由连接器建
        assert any("开会话" in line for line in AuditLog(env.paths.audit).tail())
        await probe.close()


async def test_stream_reconnect_attaches_and_resends(tmp_path: Path) -> None:
    async with running(tmp_path, Script(ask_permission=True)) as env:
        await env.register(project=str(env.root / "p"))
        probe = await env.probe()
        opened = await probe.request("session.open", {})
        await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
        await probe.wait_for(lambda inbox: any(m["method"] == "permission.ask" for m in inbox))
        processed = probe.inbox[0]["params"]["seq"]
        await probe.close()  # Agent 服务那边断了：bridge 照常运行
        await asyncio.sleep(0.1)

        again = await env.probe()
        attached = await again.request(
            "session.attach", {"bridgeInstance": opened["bridge"]["instance"], "lastSeq": processed}
        )
        assert attached["turn"] == {"turnId": "run-1", "state": "awaiting_permission"}
        ask = await again.wait_for(
            lambda inbox: next((m for m in inbox if m["method"] == "permission.ask"), None)
        )
        await again.respond(ask["id"], {"optionId": "allow"})
        assert (await again.turn_ended("run-1", timeout=15))["outcome"]["kind"] == "completed"
        await again.close()


async def test_directory_outside_the_roots_is_refused(tmp_path: Path) -> None:
    async with running(tmp_path) as env:
        outside = tmp_path / "secret"
        outside.mkdir()
        await env.register(project=str(outside), create=False)
        probe = await env.probe()
        assert await probe.closed() == 4403
        notice = next(m for m in probe.inbox if m["method"] == "device.rejected")
        assert notice["params"]["reason"] == "dir_not_allowed"
        assert "不在这台电脑允许的范围内" in notice["params"]["message"]
        assert env.host.bridges.running == []


async def test_turn_mode_above_the_cap_is_refused(tmp_path: Path) -> None:
    async with running(tmp_path, max_mode="acceptEdits") as env:
        await env.register(project=str(env.root / "p"), mode="accept_edits")
        probe = await env.probe()
        await probe.request("session.open", {})
        with contextlib.suppress(Exception):
            await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT, "mode": "auto"})
        assert await probe.closed() == 4403
        notice = next(m for m in probe.inbox if m["method"] == "device.rejected")
        assert (
            notice["params"]["reason"] == "mode_too_high"
            and "接受编辑" in notice["params"]["message"]
        )


async def test_files_through_the_gateway(tmp_path: Path) -> None:
    async with running(tmp_path) as env:
        project = env.root / "shop"
        project.mkdir()
        (project / "a.txt").write_text("hello")
        async with httpx.AsyncClient(base_url=env.gw, trust_env=False) as client:
            got = await client.post(
                f"/internal/files/{DEVICE}",
                json={"op": "fetch", "project_dir": str(project), "path": "a.txt"},
            )
            assert got.status_code == 200 and got.content == b"hello" and got.headers["etag"]
            roots = await client.post(f"/internal/files/{DEVICE}", json={"op": "roots"})
            assert [e["name"] for e in roots.json()["roots"][0]["entries"]] == ["shop"]


@pytest.fixture(autouse=True)
def _plain_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_CONNECT_PLAIN_CREDENTIALS", "1")
