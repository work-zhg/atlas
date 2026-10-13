"""RECOVERING：agent 不可用时重启并恢复同一个会话（Bridge 设计 §5.5 · §5.6 · §6.5）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from atlas_bridge.errors import SessionNotOpen
from atlas_bridge.session.host import Phase
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import SessionHarness, until
from atlas_host import OpenParams


def states(h: SessionHarness) -> list[tuple[str, dict[str, Any]]]:
    return [(p["state"], p["agent"]) for p in h.outbox.of("session.state")]


async def recovered(h: SessionHarness) -> None:
    await until(lambda: h.host.phase is Phase.READY and len(h.outbox.of("session.state")) == 2)


async def test_crash_mid_turn_recovers_the_same_session(make: Any) -> None:
    h = make(Script(on_prompt="crash"))
    h.agent.script_after_restart = Script()
    await h.open()
    sid = h.host.agent_session_id
    await h.start("t1")
    assert (await h.turn_over("t1"))["outcome"] == {"kind": "failed", "cause": "agent_exited"}
    await recovered(h)

    assert states(h) == [
        ("recovering", {"restarts": 1, "cause": "agent_exited"}),
        ("ready", {"restarts": 1, "resumed": True, "agentSessionId": sid}),
    ]
    methods = [m.method for m in h.outbox.messages]
    assert methods.index("session.state") > methods.index("turn.state")  # 先报这一轮，再报会话
    assert h.agent_calls("session/resume")[-1]["sessionId"] == sid  # 能力位有 resume：不重放
    assert h.host.agent_session_id == sid

    await h.start("t2")  # 恢复后照常工作
    assert (await h.turn_over("t2"))["outcome"]["kind"] == "completed"


async def test_crash_while_idle_recovers_too(make: Any) -> None:
    """★ 空闲时的崩溃也要告诉 server：会话刚经历了一次重启（§5.6 补充的 session.state）。"""
    h = make()
    await h.open()
    h.agent.crash()
    await recovered(h)
    assert states(h)[1][1]["resumed"] is True
    assert h.outbox.of("turn.state") == []


async def test_turn_start_during_recovery_is_refused_with_cause(make: Any) -> None:
    h = make()
    await h.open()
    h.agent.hold_restart = asyncio.Event()
    h.agent.crash()
    await until(lambda: h.host.phase is Phase.RECOVERING)
    with pytest.raises(SessionNotOpen) as info:
        await h.start()
    assert info.value.cause == "recovering"
    h.agent.hold_restart.set()
    await recovered(h)
    await h.start()
    await h.turn_over()


async def test_load_only_agent_replays_and_marks_it(make: Any) -> None:
    """只支持 load：历史照样重放，标 origin=replay 送出，由 server 丢弃（§5.6）。"""
    h = make(Script(capabilities={"loadSession": True}))
    await h.open()
    await h.start()
    await h.turn_over()
    before = len(h.outbox.of("agent.update"))
    h.agent.crash()
    await recovered(h)
    replayed = h.outbox.of("agent.update")[before:]
    assert replayed and {u["origin"] for u in replayed} == {"replay"}
    assert states(h)[1][1]["resumed"] is True


async def test_agent_without_resume_or_load_loses_context_and_says_so(make: Any) -> None:
    """★ 从不假装：新建了会话就报 resumed=false，并给出新的 agentSessionId。"""
    h = make(Script(capabilities={}))
    await h.open()
    old = h.host.agent_session_id
    h.agent.crash()
    await recovered(h)
    ready = states(h)[1][1]
    assert ready["resumed"] is False
    assert ready["agentSessionId"] == h.host.agent_session_id != old


async def test_crash_loop_ends_the_session(make: Any) -> None:
    h = make()
    h.agent.max_restarts = 0
    await h.open()
    h.agent.crash()
    await until(lambda: h.host.phase is Phase.ENDED)
    assert [s for s, _ in states(h)] == ["recovering"]
    assert h.outbox.of("session.ended")[0]["cause"] == "agent_crash_loop"
    assert h.host.ended.is_set()


async def test_agent_that_restarts_but_cannot_open_a_session_ends_in_a_crash_loop(
    make: Any,
) -> None:
    """进程起得来，但恢复与新建都失败：反复重启，用完节制后 session.ended，不会卡在 RECOVERING。"""
    h = make()
    await h.open()
    h.agent.script_after_restart = Script(on_open="error")
    h.agent.crash()
    await until(lambda: h.host.phase is Phase.ENDED)
    assert h.agent.restarts == h.agent.max_restarts
    assert [s for s, _ in states(h)] == ["recovering"]  # 从未 ready
    assert h.outbox.of("session.ended")[0]["cause"] == "agent_crash_loop"
    assert h.host.ended.is_set()


async def test_crash_before_open_reboots_back_to_idle(make: Any) -> None:
    h = make()
    await h.host.boot()
    h.agent.crash()
    await until(lambda: h.agent.restarts == 1 and h.host.phase is Phase.IDLE)
    await h.host.open(OpenParams())
    assert h.host.phase is Phase.READY
    assert h.outbox.of("session.state") == []  # 还没有会话，没什么可报告的


async def test_open_waits_for_a_reboot_in_progress(make: Any) -> None:
    h = make()
    await h.host.boot()
    h.agent.hold_restart = asyncio.Event()
    h.agent.crash()
    await until(lambda: h.host.phase is Phase.BOOTING)
    opening = asyncio.create_task(h.host.open(OpenParams()))
    await asyncio.sleep(0.01)
    assert not opening.done()
    h.agent.hold_restart.set()
    await asyncio.wait_for(opening, 2)
    assert h.host.phase is Phase.READY


async def test_close_during_recovery(make: Any) -> None:
    h = make()
    await h.open()
    h.agent.hold_restart = asyncio.Event()
    h.agent.crash()
    await until(lambda: h.host.phase is Phase.RECOVERING)
    closing = asyncio.create_task(h.host.close())
    await asyncio.sleep(0.01)
    h.agent.hold_restart.set()
    await asyncio.wait_for(closing, 2)
    await asyncio.sleep(0.01)
    assert h.host.phase is Phase.ENDED
    assert h.outbox.of("session.ended") == []  # server 要求的关闭
    assert not h.agent_calls("session/close")  # agent 不可信时不再发 session/close
