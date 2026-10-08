"""HostRuntime：server 侧对接新 bridge（代码设计 §11）。

真实的新 bridge（BridgeApp，进程内事件循环）+ 真实 agent 子进程（FakeAgent）+ 真实 WebSocket；
平台侧的审批、会话存储、取消信号用替身 —— 不碰数据库与 Redis。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_bridge.app import BridgeApp
from atlas_bridge.config import BridgeConfig
from atlas_bridge.testing.fake_agent import Script
from atlas_server.domain.events import EventType, TraceEvent
from atlas_server.domain.spec import CliSpec
from atlas_server.host.runtime import HostRuntime
from atlas_server.providers.pods import PodEndpoint

TOKEN = "tok-" + "y" * 40


# ═══════════════════════════════════ 替身 ═══════════════════════════════════


class Pods:
    def __init__(self, url: str) -> None:
        self.url = url
        self.fail: Exception | None = None

    async def ensure(self, thread: Any, cli: CliSpec) -> PodEndpoint:
        if self.fail is not None:
            raise self.fail
        return PodEndpoint(url=self.url, token=TOKEN)


class Approvals:
    """按预设的决定答复；expire 记录下来。"""

    def __init__(self, decision: str = "approved", *, after_polls: int = 1) -> None:
        self.decision = decision
        self.after_polls = after_polls
        self.polls: dict[str, int] = {}
        self.expired: list[str] = []

    async def check(self, *, approval_id: str, tool_name: str, args: dict[str, Any]) -> str:
        n = self.polls.get(approval_id, 0)
        self.polls[approval_id] = n + 1
        if approval_id in self.expired:
            return "expired"
        return self.decision if n >= self.after_polls else "pending"

    async def expire(self, approval_id: str) -> None:
        self.expired.append(approval_id)


class Sessions:
    def __init__(self) -> None:
        self.remembered: list[tuple[UUID, str]] = []

    async def remember(self, thread_id: UUID, agent_session_id: str) -> None:
        self.remembered.append((thread_id, agent_session_id))


class Relay:
    def __init__(self) -> None:
        self.cancelled = False

    async def is_cancelled(self, run_id: UUID) -> bool:
        return self.cancelled


def prepared(
    thread: Any, *, text: str = "hi", timeout_s: int = 60, permission_mode: str = "auto"
) -> Any:
    cli = CliSpec(
        cli_type="claude-code",
        adapter="x",
        image="img",
        permission_mode=permission_mode,  # type: ignore[arg-type]
    )
    spec = SimpleNamespace(
        slug="cli",
        name="CLI",
        cli=cli,
        tool_names=(),
        skills=(),
        limits=SimpleNamespace(timeout_s=timeout_s),
    )
    return SimpleNamespace(
        spec=spec,
        input_content=text,
        thread=thread,
        thread_id=thread.id,
        start_seq=0,
        base_depth=0,
    )


# ═══════════════════════════════════ 脚手架 ═══════════════════════════════════


class Env:
    def __init__(self, app: BridgeApp, thread: Any) -> None:
        self.app = app
        self.thread = thread
        self.pods = Pods(f"ws://127.0.0.1:{app.server.port}")
        self.approvals = Approvals()
        self.sessions = Sessions()
        self.relay = Relay()
        settings = SimpleNamespace(
            acp_ws_connect_timeout_s=5.0,
            acp_prompt_idle_timeout_s=120.0,
            acp_tool_idle_timeout_s=600.0,
        )
        self.settings = settings
        self.runtime = HostRuntime(
            None,  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            self.pods,
            approvals=lambda run_id, redis: self.approvals,
            sessions=self.sessions,
        )

    async def run(self, **kw: Any) -> list[TraceEvent]:
        out: list[TraceEvent] = []
        gen = self.runtime.run_turn(
            prepared(self.thread, **kw),
            run_id=uuid4(),
            redis=None,
            relay=self.relay,  # type: ignore[arg-type]
        )
        async for event in gen:
            out.append(event)
        return out


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[Env]]]:
    apps: list[tuple[BridgeApp, asyncio.Task[int]]] = []
    thread = SimpleNamespace(id=uuid4(), external_session_id=None)

    async def start(script: Script | None = None, **cfg: Any) -> Env:
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        app = BridgeApp(
            BridgeConfig(
                session_id=str(thread.id),
                token_file=token_file,
                listen_host="127.0.0.1",
                listen_port=0,
                agent_cmd=(
                    sys.executable,
                    "-m",
                    "atlas_bridge.testing.fake_agent",
                    (script or Script()).to_json(),
                ),
                agent_oom_score_adj=None,
                workspace=tmp_path,
                kill_grace_s=1,
                **cfg,
            )
        )
        task = asyncio.create_task(app.run())
        await asyncio.wait_for(app.started.wait(), 5)
        for _ in range(200):
            if app.host.phase.value != "booting":
                break
            await asyncio.sleep(0.02)
        apps.append((app, task))
        return Env(app, thread)

    yield start
    for app, task in apps:
        app.stop()
        await asyncio.wait_for(task, 10)


def types(events: list[TraceEvent]) -> list[str]:
    return [e.type.value for e in events]


def last(events: list[TraceEvent]) -> TraceEvent:
    return events[-1]


# ═══════════════════════════════════ 用例 ═══════════════════════════════════


async def test_a_full_turn(env: Any) -> None:
    e = await env()
    events = await e.run()
    assert types(events) == [
        "run.started",
        "agent.mode",  # 本轮实际生效的权限模式（默认 auto）
        "message.delta",
        "message.delta",
        "message.completed",
        "usage.updated",
        "run.finished",
    ]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert "第 1 段第 2 段" in str(events[4].data["content"])
    assert last(events).data["stop_reason"] == "end_turn"
    assert last(events).data["text_len"] == len("第 1 段第 2 段")
    # 会话身份当场落下：下一轮的恢复全靠它
    assert e.sessions.remembered == [(e.thread.id, e.app.host.agent_session_id)]


async def test_second_run_on_the_same_pod(env: Any) -> None:
    """Pod 上的会话已由前一轮打开：session.open 返回 ALREADY_OPEN，照常开下一轮。"""
    e = await env()
    await e.run()
    e.thread.external_session_id = e.sessions.remembered[-1][1]
    events = await e.run(text="again")
    assert last(events).type is EventType.RUN_FINISHED
    assert EventType.SESSION_LOST not in [x.type for x in events]


async def test_resume_on_a_new_pod_that_lost_the_session_is_visible(env: Any) -> None:
    """新 Pod 上恢复不了（FakeAgent 不持久化会话）：必须以 session.lost 让用户看见。"""
    e = await env()
    e.thread.external_session_id = "gone-session"
    events = await e.run()
    assert EventType.SESSION_LOST in [x.type for x in events]
    assert last(events).type is EventType.RUN_FINISHED
    assert e.sessions.remembered[-1][1] != "gone-session"


async def test_permission_approved(env: Any) -> None:
    e = await env(Script(ask_permission=True))
    events = await e.run()
    kinds = types(events)
    assert "approval.required" in kinds
    required = next(x for x in events if x.type is EventType.APPROVAL_REQUIRED)
    assert required.data["tool_name"] == "写入 /workspace/a.txt"
    assert kinds.index("approval.required") > kinds.index("tool.started")
    assert "tool.completed" in kinds and "tool.failed" not in kinds
    assert last(events).type is EventType.RUN_FINISHED


async def test_permission_rejected_says_why(env: Any) -> None:
    e = await env(Script(ask_permission=True))
    e.approvals.decision = "rejected"
    events = await e.run()
    failed = [x for x in events if x.type is EventType.TOOL_FAILED]
    assert failed and failed[0].data["result"] == "用户拒绝了这次调用。"
    assert last(events).type is EventType.RUN_FINISHED  # 正文不空：不是「挡下且没说话」


async def test_an_ask_waits_for_the_human_without_expiry(env: Any) -> None:
    """★ 审批只由人决定：没有等待上限，系统不替用户按拒绝处理。"""
    e = await env(Script(ask_permission=True))
    e.approvals.after_polls = 10_000  # 先没人答复

    async def approve_later() -> None:
        while not e.approvals.polls:
            await asyncio.sleep(0.02)
        await asyncio.sleep(2.5)  # 比原先的任何过期都长（测试里的旧值是 0.5s）
        e.approvals.after_polls = 0

    helper = asyncio.create_task(approve_later())
    events = await e.run()
    await helper
    assert e.approvals.expired == []
    assert "tool.completed" in types(events) and "tool.failed" not in types(events)
    assert last(events).type is EventType.RUN_FINISHED


async def test_cancel_while_awaiting_approval_withdraws_the_ask(env: Any) -> None:
    """取消时询问随这一轮撤回：审批行作废（弹窗消失），不是替用户做了决定。"""
    e = await env(Script(ask_permission=True))
    e.approvals.after_polls = 10_000

    async def cancel_while_waiting() -> None:
        while not e.approvals.polls:
            await asyncio.sleep(0.02)
        e.relay.cancelled = True

    helper = asyncio.create_task(cancel_while_waiting())
    events = await e.run()
    await helper
    assert last(events).type is EventType.RUN_CANCELLED
    assert len(e.approvals.expired) == 1


async def test_silent_rejection_is_a_failure(env: Any) -> None:
    """工具被挡下且一个字没说：不是成功收尾（CLI 报 end_turn 只表示讲完了，不表示做成了）。"""
    e = await env(Script(ask_permission=True, updates_per_turn=0))
    e.approvals.decision = "rejected"
    events = await e.run()
    assert last(events).type is EventType.RUN_FAILED
    assert last(events).data["error_kind"] == "subagent_silent"


async def test_cancel(env: Any) -> None:
    e = await env(Script(on_prompt="silent"))

    async def cancel_soon() -> None:
        await asyncio.sleep(0.5)
        e.relay.cancelled = True

    asyncio.create_task(cancel_soon())  # noqa: RUF006
    events = await e.run()
    assert last(events).type is EventType.RUN_CANCELLED


async def test_a_turn_has_no_time_limit(env: Any) -> None:
    """★ CLI 在干活就不中断：spec 的 timeout_s 不下发为截止，超过它照样在跑，直到用户取消。"""
    e = await env(Script(on_prompt="silent"))

    async def cancel_later() -> None:
        await asyncio.sleep(2.5)  # 远超 timeout_s=1
        e.relay.cancelled = True

    helper = asyncio.create_task(cancel_later())
    events = await e.run(timeout_s=1)
    await helper
    assert last(events).type is EventType.RUN_CANCELLED


async def test_open_sends_only_the_idle_thresholds(env: Any) -> None:
    """截止、审批等待、重连窗口都不下发（= 不限）；静默阈值来自 server 配置。"""
    e = await env()
    e.settings.acp_prompt_idle_timeout_s = 77.0
    e.settings.acp_tool_idle_timeout_s = 777.0
    await e.run()
    limits = e.app.host.limits
    assert (limits.idle_s, limits.tool_idle_s) == (77.0, 777.0)
    assert limits.deadline_s is None
    assert limits.permission_wait_s is None
    assert limits.reconnect_window_s is None


async def test_agent_crash_mid_turn(env: Any) -> None:
    e = await env(Script(on_prompt="crash"))
    events = await e.run()
    assert last(events).type is EventType.RUN_FAILED
    assert last(events).data["error_kind"] == "runtime_crashed"


async def test_pod_unavailable(env: Any) -> None:
    e = await env()
    e.pods.fail = RuntimeError("quota")
    events = await e.run()
    assert last(events).data["error_kind"] == "pod_unavailable"


async def test_bridge_unreachable(env: Any) -> None:
    e = await env()
    e.pods.url = "ws://127.0.0.1:1"
    events = await e.run()
    assert last(events).data["error_kind"] == "runtime_unreachable"


async def test_wrong_token_is_unreachable_not_a_hang(env: Any) -> None:
    e = await env()
    global TOKEN
    real, TOKEN = TOKEN, "wrong"
    try:
        events = await e.run()
    finally:
        TOKEN = real
    assert last(events).data["error_kind"] == "runtime_unreachable"
    assert "401" in last(events).data["message"]


# ═══════════════════════════════════ 断线、重连与 attach（§7.3 · §7.5 · §7.6）═══════════════════


async def test_next_run_attaches_instead_of_opening(env: Any) -> None:
    e = await env()
    await e.run()
    known = e.runtime._known[e.thread.id]
    assert known.instance == e.app.instance and known.last_seq > 0
    events = await e.run(text="again")
    assert last(events).type is EventType.RUN_FINISHED
    assert e.app.outbox.acked == e.app.outbox.last_seq  # 全部确认，bridge 缓冲已清空
    assert e.app.outbox.retained == 0


async def test_disconnect_mid_turn_reconnects_and_the_approval_is_not_duplicated(env: Any) -> None:
    """★ 等人时断线：重连 attach，询问以同一 id 补发，沿用同一条审批、不重复弹窗。"""
    e = await env(Script(ask_permission=True))
    e.approvals.after_polls = 10_000  # 先没人答复

    async def disconnect_then_approve() -> None:
        while not e.approvals.polls:
            await asyncio.sleep(0.02)
        await e.app.server.close_current(1011, "test: network blip")
        await asyncio.sleep(1.0)
        e.approvals.after_polls = 0  # 重连之后有人批准了

    helper = asyncio.create_task(disconnect_then_approve())
    events = await e.run()
    await helper
    assert types(events).count("approval.required") == 1
    assert len(e.approvals.polls) == 1  # 只登记过一条审批
    assert e.approvals.expired == []
    assert "tool.completed" in types(events)
    assert last(events).type is EventType.RUN_FINISHED
    assert [x.seq for x in events] == list(range(1, len(events) + 1))


async def test_a_long_outage_is_ridden_out_and_the_turn_completes(env: Any) -> None:
    """★ 重连没有窗口：断线期间连 Pod 都拿不到（ensure 失败），恢复后照样接上、跑完。"""
    e = await env(Script(ask_permission=True))
    e.approvals.after_polls = 10_000

    async def outage() -> None:
        while not e.approvals.polls:
            await asyncio.sleep(0.02)
        e.pods.fail = RuntimeError("cluster 暂时不可用")
        await e.app.server.close_current(1011, "test: outage")
        await asyncio.sleep(2.0)
        e.pods.fail = None
        e.approvals.after_polls = 0

    helper = asyncio.create_task(outage())
    events = await e.run()
    await helper
    assert last(events).type is EventType.RUN_FINISHED
    assert types(events).count("approval.required") == 1
    assert [x.seq for x in events] == list(range(1, len(events) + 1))


async def test_cancel_during_an_outage_ends_the_run_as_cancelled(env: Any) -> None:
    e = await env(Script(on_prompt="silent"))

    async def outage_then_cancel() -> None:
        while e.app.host.turn is None:
            await asyncio.sleep(0.02)
        e.pods.fail = RuntimeError("cluster 暂时不可用")
        await e.app.server.close_current(1011, "test: outage")
        await asyncio.sleep(1.0)
        e.relay.cancelled = True

    helper = asyncio.create_task(outage_then_cancel())
    events = await e.run()
    await helper
    assert last(events).type is EventType.RUN_CANCELLED


async def test_bridge_restarted_mid_turn_is_bridge_lost(env: Any) -> None:
    """一轮进行中断线，重连时发现 bridge 重启过（实例 id 变了）：这一轮的进度已丢失（§7.6）。"""
    e = await env(Script(on_prompt="silent"))

    async def restart_then_disconnect() -> None:
        while e.thread.id not in e.runtime._known or e.app.host.turn is None:
            await asyncio.sleep(0.02)
        # 模拟：runtime 记得的是重启前的实例
        e.runtime._known[e.thread.id].instance = "b-before-restart"
        await e.app.server.close_current(1011, "test")

    task = asyncio.create_task(restart_then_disconnect())
    events = await e.run(timeout_s=30)
    await task
    assert last(events).data["error_kind"] == "bridge_lost"
    assert e.thread.id not in e.runtime._known  # 忘掉它，下一轮 session.open


async def test_superseded_connection_does_not_reconnect(env: Any) -> None:
    from atlas_bridge.testing.probe import UpstreamProbe

    e = await env(Script(on_prompt="silent"))

    async def take_over() -> None:
        await asyncio.sleep(0.5)
        probe = await UpstreamProbe.connect(
            f"ws://127.0.0.1:{e.app.server.port}/host", token=TOKEN, session_id=str(e.thread.id)
        )
        await asyncio.sleep(1)
        await probe.close()

    task = asyncio.create_task(take_over())
    events = await e.run(timeout_s=30)
    await task
    assert last(events).data["error_kind"] == "runtime_unreachable"
    assert "取代" in last(events).data["message"]


async def test_server_that_forgot_the_bridge_attaches_via_already_open(env: Any) -> None:
    """server 重启过（进程内记忆没了）：open 得到 ALREADY_OPEN，带回实例 id，改用 attach。"""
    e = await env()
    await e.run()
    e.runtime._known.clear()
    e.thread.external_session_id = e.sessions.remembered[-1][1]
    events = await e.run(text="again")
    assert last(events).type is EventType.RUN_FINISHED
    assert e.runtime._known[e.thread.id].instance == e.app.instance


# ═════════════════════════ 权限模式（doc/acp-permission-mode-design.html）═════════════════════════


def mode_events(events: list[TraceEvent]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type is EventType.AGENT_MODE]


async def test_auto_is_the_default_and_reaches_the_agent(env: Any) -> None:
    """spec 不写 permission_mode 时就是 auto：下发给 bridge、CLI 实际切到 auto、事件如实回报。"""
    e = await env()
    events = await e.run()
    assert last(events).type is EventType.RUN_FINISHED
    assert events[0].data["permission_mode"] == "auto"  # run.started 留审计记录
    assert mode_events(events) == [{"requested": "auto", "effective": "auto", "degraded": False}]
    assert e.app.host._mode_current == "auto"


async def test_platform_mode_names_are_mapped_both_ways(env: Any) -> None:
    e = await env()
    events = await e.run(permission_mode="accept_edits")
    assert mode_events(events) == [
        {"requested": "accept_edits", "effective": "accept_edits", "degraded": False}
    ]
    assert e.app.host._mode_current == "acceptEdits"  # 下发的是 ACP modeId


async def test_degraded_auto_is_visible(env: Any) -> None:
    """adapter 把 auto 降级成 acceptEdits：这一轮照常完成，事件里标明降级。"""
    e = await env(Script(mode_fallback={"auto": "acceptEdits"}))
    events = await e.run()
    assert last(events).type is EventType.RUN_FINISHED
    assert mode_events(events) == [
        {"requested": "auto", "effective": "accept_edits", "degraded": True}
    ]


async def test_failing_to_tighten_is_a_clear_failure(env: Any) -> None:
    """要 plan 却设不上：不以更宽松的模式执行，run 以 mode_unavailable 失败并说明。"""
    e = await env(Script(initial_mode="acceptEdits", set_mode_fails=True))
    events = await e.run(permission_mode="plan")
    assert last(events).type is EventType.RUN_FAILED
    assert last(events).data["error_kind"] == "mode_unavailable"
    assert "权限模式" in last(events).data["message"]
