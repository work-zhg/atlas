"""AgentSupervisor：对真实子进程测试（代码设计 §7.4）。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from atlas_bridge.agent.process import AgentLaunch
from atlas_bridge.agent.supervisor import AgentSupervisor, RestartPolicy
from atlas_bridge.errors import AgentUnavailable
from atlas_bridge.testing.clock import FakeClock
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import until
from atlas_host import FailCause

NO_WAIT = RestartPolicy(max_restarts=3, window_s=600, backoff_s=(0,))


class Events:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.lost: list[FailCause] = []

    async def on_agent_update(self, raw: dict[str, Any]) -> None:
        self.updates.append(raw)

    async def on_permission_request(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"outcome": {"outcome": "cancelled"}}

    def on_agent_lost(self, cause: FailCause) -> None:
        self.lost.append(cause)


def supervisor(
    tmp_path: Path, script: Script | None = None, policy: RestartPolicy = NO_WAIT
) -> tuple[AgentSupervisor, Events, FakeClock]:
    launch = AgentLaunch(
        argv=(
            sys.executable,
            "-m",
            "atlas_bridge.testing.fake_agent",
            (script or Script()).to_json(),
        ),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        oom_score_adj=None,
    )
    clock = FakeClock()
    sup = AgentSupervisor(launch, clock, policy=policy, boot_timeout_s=10, kill_grace_s=1)
    events = Events()
    sup.bind(events)
    return sup, events, clock


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_boot_and_unexpected_exit_is_reported_once(tmp_path: Path) -> None:
    sup, events, _ = supervisor(tmp_path)
    negotiated = await sup.boot()
    assert negotiated.caps.load_session
    sid = await sup.client.new_session("/w", [], timeout=5)
    assert sid
    assert sup.pid is not None
    os.kill(sup.pid, signal.SIGKILL)
    await until(lambda: events.lost == [FailCause.AGENT_EXITED], timeout=5)
    await asyncio.sleep(0.05)
    assert events.lost == [FailCause.AGENT_EXITED]
    await sup.shutdown()


async def test_restart_replaces_the_process_without_reporting_an_exit(tmp_path: Path) -> None:
    sup, events, _ = supervisor(tmp_path)
    await sup.boot()
    old = sup.pid
    assert old is not None
    await sup.restart("test")
    assert sup.pid != old and not alive(old)
    assert sup.restarts == 1
    assert (await sup.client.new_session("/w", [], timeout=5)).startswith("fake-")
    assert events.lost == []  # 主动终止不是意外退出
    await sup.shutdown()
    assert events.lost == []


async def test_restart_throttle(tmp_path: Path) -> None:
    """10 分钟内最多重启 3 次；窗口滑过之后恢复（§5.6）。"""
    sup, _, clock = supervisor(tmp_path)
    await sup.boot()
    for _ in range(3):
        await sup.restart("crash")
    with pytest.raises(AgentUnavailable) as info:
        await sup.restart("crash")
    assert info.value.cause == "agent_crash_loop"
    clock.advance(600)
    await sup.restart("crash")  # 窗口滑过
    await sup.shutdown()


async def test_backoff_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sup, _, _ = supervisor(tmp_path, policy=RestartPolicy(backoff_s=(1, 5, 30)))
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("atlas_bridge.agent.supervisor.asyncio.sleep", fake_sleep)
    await sup.boot()
    for _ in range(3):
        await sup.restart("crash")
    assert slept == [1, 5, 30]
    await sup.shutdown()


async def test_boot_failure_is_retried_then_given_up(tmp_path: Path) -> None:
    """版本协商不通过的 agent：按节制重试，最后放弃（预热失败也计入节制）。"""
    sup, events, _ = supervisor(tmp_path, Script(protocol_version=2))
    with pytest.raises(AgentUnavailable) as info:
        await sup.boot()
    assert info.value.cause == "agent_crash_loop"
    assert events.lost == []  # 预热期间的退出不报告


async def test_missing_executable_is_a_boot_failure(tmp_path: Path) -> None:
    launch = AgentLaunch(argv=("/nonexistent/agent",), cwd=tmp_path, env={}, oom_score_adj=None)
    sup = AgentSupervisor(launch, FakeClock(), policy=NO_WAIT)
    sup.bind(Events())
    with pytest.raises(AgentUnavailable):
        await sup.boot()


async def test_updates_reach_the_callbacks(tmp_path: Path) -> None:
    sup, events, _ = supervisor(tmp_path)
    await sup.boot()
    sid = await sup.client.new_session("/w", [], timeout=5)
    future = await sup.client.start_prompt(sid, [{"type": "text", "text": "hi"}])
    assert (await asyncio.wait_for(future, 5))["stopReason"] == "end_turn"
    assert len(events.updates) == 2
    await sup.shutdown()
