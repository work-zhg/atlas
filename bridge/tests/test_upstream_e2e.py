"""集成：真实 WebSocket + 真实 agent 子进程（FakeAgent）上跑通一轮（代码设计 §12.3）。

BridgeApp 在测试的事件循环里运行，监听随机端口；agent 是独立进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from atlas_bridge.agent.supervisor import RestartPolicy
from atlas_bridge.app import BridgeApp
from atlas_bridge.config import BridgeConfig
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.probe import RpcError, UpstreamProbe
from atlas_host import CloseCode, HostErrorCode
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

TOKEN = "t0ken-" + "x" * 40
SESSION = "hs-test"
PROMPT = [{"type": "text", "text": "hi"}]


class Running:
    def __init__(self, app: BridgeApp, task: asyncio.Task[int]) -> None:
        self.app = app
        self.task = task
        self.url = f"ws://127.0.0.1:{app.server.port}/host"
        self.http = f"http://127.0.0.1:{app.server.port}"

    async def probe(self, **kw: Any) -> UpstreamProbe:
        return await UpstreamProbe.connect(
            self.url, token=kw.pop("token", TOKEN), session_id=kw.pop("session_id", SESSION), **kw
        )


def fake_agent_cmd(script: Script | None = None) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "atlas_bridge.testing.fake_agent",
        (script or Script()).to_json(),
    )


@pytest.fixture
async def bridge(tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[Running]]]:
    started: list[Running] = []

    async def start(
        script: Script | None = None,
        *,
        policy: RestartPolicy | None = None,
        ready: bool = True,
        **cfg: Any,
    ) -> Running:
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        config = BridgeConfig(
            session_id=SESSION,
            token_file=token_file,
            listen_host="127.0.0.1",
            listen_port=0,
            agent_cmd=cfg.pop("agent_cmd", None) or fake_agent_cmd(script),
            agent_oom_score_adj=None,
            workspace=tmp_path,
            kill_grace_s=1,
            **cfg,
        )
        app = BridgeApp(config)
        if policy is not None:
            # 默认节制的退避是 1 s、5 s、30 s：测「起不来」「反复崩溃」时要收紧
            app.agent._policy = policy
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.started.wait(), 5)
        running = Running(app, task)
        started.append(running)
        if ready:
            await wait_ready(running)
        return running

    yield start
    for running in started:
        running.app.stop()
        await asyncio.wait_for(running.task, 10)


async def http_get(url: str) -> tuple[int, str]:
    host, _, rest = url.removeprefix("http://").partition("/")
    name, _, port = host.partition(":")
    reader, writer = await asyncio.open_connection(name, int(port))
    writer.write(f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    data = await reader.read()
    writer.close()
    status_line, _, body = data.decode().partition("\r\n")
    return int(status_line.split()[1]), body.split("\r\n\r\n", 1)[-1]


async def wait_ready(running: Running) -> None:
    for _ in range(200):
        if (await http_get(running.http + "/readyz"))[0] == 200:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("bridge 没有就绪")


# ═══════════════════════════════════ 握手与健康检查 ═══════════════════════════════════


async def test_health_endpoints(bridge: Any) -> None:
    b = await bridge()
    assert await http_get(b.http + "/healthz") == (200, "ok\n")
    assert await http_get(b.http + "/readyz") == (200, "idle\n")
    assert (await http_get(b.http + "/nope"))[0] == 404


@pytest.mark.parametrize(
    ("kw", "status"),
    [
        ({"token": "wrong"}, 401),
        ({"session_id": "other-session"}, 403),
        ({"subprotocols": ("atlas.host.v9",)}, 426),
    ],
)
async def test_handshake_is_rejected_before_upgrade(bridge: Any, kw: Any, status: int) -> None:
    b = await bridge()
    with pytest.raises(InvalidStatus) as info:
        await b.probe(**kw)
    assert info.value.response.status_code == status


async def test_missing_token_is_401(bridge: Any) -> None:
    b = await bridge()
    with pytest.raises(InvalidStatus) as info:
        await connect(b.url, subprotocols=["atlas.host.v1"])  # type: ignore[list-item]
    assert info.value.response.status_code == 401


async def test_subprotocol_is_negotiated(bridge: Any) -> None:
    b = await bridge()
    probe = await b.probe(subprotocols=("atlas.host.v9", "atlas.host.v1"))
    assert probe.ws.subprotocol == "atlas.host.v1"
    await probe.close()


# ═══════════════════════════════════ 一轮 ═══════════════════════════════════


async def test_a_full_turn_over_websocket(bridge: Any) -> None:
    b = await bridge()
    probe = await b.probe()
    opened = await probe.request("session.open", {"defaults": {"deadlineS": 900}})
    assert opened["resumed"] is False and opened["replayed"] == "none"
    assert opened["acp"]["protocolVersion"] == 1
    assert opened["acp"]["agentInfo"]["name"] == "fake-agent"
    assert opened["bridge"]["instance"] == b.app.instance

    assert await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT}) == {
        "accepted": True
    }
    ended = await probe.turn_ended("run-1")
    assert ended["outcome"] == {
        "kind": "completed",
        "stopReason": "end_turn",
        "response": {"stopReason": "end_turn"},
    }

    kinds = [
        (m["method"], m["params"].get("state") or m["params"].get("origin")) for m in probe.inbox
    ]
    assert kinds == [
        ("turn.state", "running"),
        ("agent.update", "turn"),
        ("agent.update", "turn"),
        ("turn.state", "ended"),
    ]
    seqs = [m["params"]["seq"] for m in probe.inbox]
    assert seqs == list(range(1, len(seqs) + 1))  # 从 1 开始、严格递增、不跳号
    assert probe.of("agent.update")[0]["update"]["update"]["content"]["text"] == "第 1 段"

    await probe.ack(seqs[-1])
    await asyncio.sleep(0.05)
    assert b.app.outbox.retained == 0
    await probe.close()


async def test_permission_round_trip_over_websocket(bridge: Any) -> None:
    b = await bridge(Script(ask_permission=True))
    probe = await b.probe()
    await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    ask = await probe.wait_for(
        lambda inbox: next((m for m in inbox if m["method"] == "permission.ask"), None)
    )
    assert isinstance(ask["id"], int)
    assert ask["params"]["request"]["options"][0]["optionId"] == "allow"
    assert "expiresInS" not in ask["params"]  # 默认不过期：一直等到人做决定
    await probe.respond(ask["id"], {"optionId": "allow"})
    ended = await probe.turn_ended("run-1")
    assert ended["outcome"]["kind"] == "completed"
    last_update = probe.of("agent.update")[-1]["update"]["update"]
    assert last_update["status"] == "completed"  # agent 收到了批准
    states = [p["state"] for p in probe.of("turn.state")]
    assert states == ["running", "awaiting_permission", "running", "ended"]
    await probe.close()


async def test_cancel_over_websocket(bridge: Any) -> None:
    b = await bridge(Script(on_prompt="silent"))
    probe = await b.probe()
    await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    assert await probe.request("turn.cancel", {"turnId": "run-1"}) == {"state": "cancelling"}
    ended = await probe.turn_ended("run-1")
    assert ended["outcome"]["kind"] == "cancelled"
    assert ended["outcome"]["cause"] == "requested"
    await probe.close()


# ═══════════════════════════════════ 错误码 ═══════════════════════════════════


async def test_error_codes(bridge: Any) -> None:
    b = await bridge(Script(on_prompt="silent"))
    probe = await b.probe()

    with pytest.raises(RpcError) as info:
        await probe.request("turn.start", {"turnId": "r", "prompt": PROMPT})
    assert info.value.code == -32010 and info.value.data["cause"] == "not_opened"
    assert "detail" in info.value.data

    await probe.request("session.open", {})
    with pytest.raises(RpcError) as info:
        await probe.request("session.open", {})
    assert info.value.code == -32011

    with pytest.raises(RpcError) as info:
        await probe.request("turn.start", {"turnId": "r", "prompt": []})  # prompt 不能为空
    assert info.value.code == -32602 and info.value.data["cause"] == "invalid_params"

    await probe.request("turn.start", {"turnId": "r1", "prompt": PROMPT})
    with pytest.raises(RpcError) as info:
        await probe.request("turn.start", {"turnId": "r2", "prompt": PROMPT})
    assert info.value.code == -32012 and info.value.data["turnId"] == "r1"

    with pytest.raises(RpcError) as info:
        await probe.request("turn.cancel", {"turnId": "nope"})
    assert info.value.code == -32013

    with pytest.raises(RpcError) as info:
        await probe.request("session.frobnicate", {})
    assert info.value.code == -32601
    await probe.close()


# ═══════════════════════════════════ 连接 ═══════════════════════════════════


async def test_new_connection_supersedes_the_old_one(bridge: Any) -> None:
    b = await bridge()
    first = await b.probe()
    second = await b.probe()
    assert await first.closed() == CloseCode.SUPERSEDED
    await second.request("session.open", {})  # 新连接照常工作
    await second.close()


async def test_session_close_closes_with_4410_and_later_connections_too(bridge: Any) -> None:
    b = await bridge(Script(on_prompt="silent"))
    probe = await b.probe()
    await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    assert await probe.request("session.close", {}) == {}
    # 进行中的一轮先按取消流程结束，ended 排在 close 的响应之前
    assert (await probe.turn_ended("run-1"))["outcome"]["cause"] == "session_closing"
    assert await probe.closed() == CloseCode.SESSION_GONE

    again = await b.probe()
    assert await again.closed() == CloseCode.SESSION_GONE
    assert not b.task.done()  # 进程不退出，等 Pod 回收


async def test_turn_survives_a_disconnect_and_attach_resends(bridge: Any) -> None:
    """★ 断线不结束这一轮（§7.2）；重连后 session.attach，补发 server 没处理完的消息（§7.3）。"""
    b = await bridge(Script(ask_permission=True))
    probe = await b.probe()
    opened = await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    await probe.wait_for(lambda inbox: any(m["method"] == "permission.ask" for m in inbox))
    processed = probe.inbox[0]["params"]["seq"]  # 假装 server 只处理完了第一条
    await probe.close()
    await asyncio.sleep(0.05)

    again = await b.probe()
    await asyncio.sleep(0.05)
    assert again.inbox == []  # ★ 在 attach 之前什么都不发
    attached = await again.request(
        "session.attach", {"bridgeInstance": opened["bridge"]["instance"], "lastSeq": processed}
    )
    assert attached["state"] == "in_turn"
    assert attached["turn"] == {"turnId": "run-1", "state": "awaiting_permission"}
    assert attached["resendFrom"] == processed + 1 and attached["gaps"] == []
    # 补发：processed 之后的全部，包括那个询问（沿用原 id）
    ask = await again.wait_for(
        lambda inbox: next((m for m in inbox if m["method"] == "permission.ask"), None)
    )
    seqs = [m["params"]["seq"] for m in again.inbox]
    assert seqs[0] == processed + 1 and seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert again.frames[0].get("result") == attached  # 响应排在补发之前

    await again.respond(ask["id"], {"optionId": "allow"})
    ended = await again.turn_ended("run-1")
    assert ended["outcome"]["kind"] == "completed"
    await again.close()


async def test_attach_to_a_restarted_bridge_is_refused(bridge: Any) -> None:
    b = await bridge()
    probe = await b.probe()
    await probe.request("session.open", {})
    with pytest.raises(RpcError) as info:
        await probe.request("session.attach", {"bridgeInstance": "b-old", "lastSeq": 0})
    assert info.value.code == -32010
    assert info.value.data["cause"] == "bridge_restarted"
    assert info.value.data["bridgeInstance"] == b.app.instance
    await probe.close()


async def test_already_open_tells_the_instance(bridge: Any) -> None:
    b = await bridge()
    probe = await b.probe()
    opened = await probe.request("session.open", {})
    with pytest.raises(RpcError) as info:
        await probe.request("session.open", {})
    assert info.value.data["bridgeInstance"] == b.app.instance
    assert info.value.data["agentSessionId"] == opened["agentSessionId"]
    await probe.close()


async def test_turn_is_cancelled_when_upstream_does_not_come_back(bridge: Any) -> None:
    """重连窗口到期仍没有回来：以 upstream_lost 取消这一轮，结果留在缓冲里等 server 取。"""
    b = await bridge(Script(on_prompt="silent"))
    probe = await b.probe()
    opened = await probe.request("session.open", {"defaults": {"reconnectWindowS": 0.3}})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    await probe.close()
    for _ in range(100):
        if "run-1" in b.app.host.ledger:
            break
        await asyncio.sleep(0.05)
    again = await b.probe()
    await again.request(
        "session.attach", {"bridgeInstance": opened["bridge"]["instance"], "lastSeq": 0}
    )
    ended = await again.turn_ended("run-1")
    assert ended["outcome"]["cause"] == "upstream_lost"
    await again.close()


async def test_agent_crash_is_recovered_over_websocket(bridge: Any) -> None:
    b = await bridge()
    probe = await b.probe()
    opened = await probe.request("session.open", {})
    pid = b.app.agent.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    ready = await probe.wait_for(
        lambda inbox: next(
            (
                m["params"]
                for m in inbox
                if m["method"] == "session.state" and m["params"]["state"] == "ready"
            ),
            None,
        ),
        timeout=15,
    )
    # 独立进程的 FakeAgent 不把会话写到磁盘：重启后恢复不了，bridge 新建会话并如实报告
    assert ready["agent"]["restarts"] == 1
    assert ready["agent"]["resumed"] is False
    assert ready["agent"]["agentSessionId"] != opened["agentSessionId"]
    await probe.request("turn.start", {"turnId": "run-2", "prompt": PROMPT})
    assert (await probe.turn_ended("run-2"))["outcome"]["kind"] == "completed"
    await probe.close()


# ═══════════════════════════════════ 进程级：启动、open 失败、会话结束、入口 ═══════════════════


async def test_boot_failure_is_not_ready_meanwhile_and_exits_with_1(bridge: Any) -> None:
    """★ agent 预热失败（适配器路径错、initialize 无响应即退出）：
    期间 /readyz 503，用完节制后退出码 1。"""
    b = await bridge(
        agent_cmd=(sys.executable, "-c", "import time; time.sleep(0.3)"),
        policy=RestartPolicy(max_restarts=1, backoff_s=(0.2,)),
        ready=False,
    )
    assert await http_get(b.http + "/readyz") == (503, "booting\n")
    assert await asyncio.wait_for(b.task, 10) == 1


async def test_open_timeout_over_websocket_reboots_and_becomes_ready_again(bridge: Any) -> None:
    b = await bridge(Script(on_open="silent"), open_timeout_s=0.3)
    probe = await b.probe()
    with pytest.raises(RpcError) as info:
        await probe.request("session.open", {})
    assert info.value.code == HostErrorCode.OPEN_TIMEOUT
    assert info.value.data["cause"] == "open_timeout"
    # 重启 agent 期间不就绪（重启前至少退避 1 s），之后回到 idle，server 可以重试
    assert await http_get(b.http + "/readyz") == (503, "booting\n")
    await wait_ready(b)
    assert await http_get(b.http + "/readyz") == (200, "idle\n")
    assert b.app.agent.restarts == 1
    await probe.close()


async def test_crash_loop_ends_the_session_with_4410_and_the_process_stays(bridge: Any) -> None:
    """bridge 自己结束会话（不是 server 要求的）：先发 session.ended，
    再以 4410 关闭；进程不退出。"""
    b = await bridge(Script(on_prompt="crash"), policy=RestartPolicy(max_restarts=0))
    probe = await b.probe()
    await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
    ended = await probe.turn_ended("run-1")
    assert ended["outcome"] == {"kind": "failed", "cause": "agent_exited"}
    gone = await probe.wait_for(
        lambda inbox: next((m["params"] for m in inbox if m["method"] == "session.ended"), None)
    )
    assert gone["cause"] == "agent_crash_loop"
    assert await probe.closed() == CloseCode.SESSION_GONE

    again = await b.probe()
    assert await again.closed() == CloseCode.SESSION_GONE
    assert not b.task.done()  # 退出会让容器被重启；这个 Pod 只等回收


async def test_entrypoint_reads_env_serves_and_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    """``python -m atlas_bridge``：容器里的真实启动方式。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    env = {
        **os.environ,
        "ATLAS_BRIDGE_SESSION_ID": SESSION,
        "ATLAS_BRIDGE_TOKEN_FILE": str(token_file),
        "ATLAS_BRIDGE_LISTEN_HOST": "127.0.0.1",
        "ATLAS_BRIDGE_LISTEN_PORT": str(port),
        "ATLAS_BRIDGE_AGENT_CMD": json.dumps(fake_agent_cmd()),
        "ATLAS_BRIDGE_AGENT_OOM_SCORE_ADJ": "",
        "ATLAS_BRIDGE_WORKSPACE": str(tmp_path),
    }
    with (tmp_path / "bridge.log").open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "atlas_bridge", env=env, stdout=log, stderr=log
        )
    try:
        http = f"http://127.0.0.1:{port}"
        for _ in range(500):
            with contextlib.suppress(OSError):
                if (await http_get(http + "/readyz"))[0] == 200:
                    break
            await asyncio.sleep(0.02)
        else:
            raise TimeoutError((tmp_path / "bridge.log").read_text())

        probe = await UpstreamProbe.connect(
            f"ws://127.0.0.1:{port}/host", token=TOKEN, session_id=SESSION
        )
        await probe.request("session.open", {})
        await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT})
        assert (await probe.turn_ended("run-1"))["outcome"]["kind"] == "completed"

        proc.send_signal(signal.SIGTERM)
        assert await probe.closed() == CloseCode.GOING_AWAY
        assert await asyncio.wait_for(proc.wait(), 10) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_permission_mode_round_trip_over_websocket(bridge: Any) -> None:
    """turn.start 带 mode：bridge 设置模式后发 prompt，running 报告实际生效的模式。"""
    b = await bridge(Script(mode_fallback={"auto": "acceptEdits"}))
    probe = await b.probe()
    await probe.request("session.open", {})
    await probe.request("turn.start", {"turnId": "run-1", "prompt": PROMPT, "mode": "auto"})
    assert (await probe.turn_ended("run-1"))["outcome"]["kind"] == "completed"
    running = next(p for p in probe.of("turn.state") if p["state"] == "running")
    assert running["mode"]["requested"] == "auto"
    assert running["mode"]["effective"] == "acceptEdits"  # 子进程里的 adapter 自行降级了
    assert running["mode"]["degraded"] is True
    await probe.close()
