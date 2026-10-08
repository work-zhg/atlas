"""HostSession + Turn 的组件测试：进程内 FakeAgent + FakeClock + RecordingOutbox。

没有进程、没有网络、没有真实时间：计时全部由 FakeClock 推进。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from atlas_bridge.errors import (
    AcpRequestTimeout,
    AgentUnavailable,
    BridgeError,
    SessionAlreadyOpen,
    SessionNotOpen,
    TurnBusy,
    TurnNotFound,
)
from atlas_bridge.session.host import Phase
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import until
from atlas_host import OpenParams, ResumeSpec, TurnLimits

# ═══════════════════════════════════ open ═══════════════════════════════════


async def test_open_new_session(make: Any) -> None:
    h = make()
    await h.host.boot()
    assert h.host.phase is Phase.IDLE
    result = await h.host.open(OpenParams(defaults=TurnLimits(deadline_s=900)))
    assert h.host.phase is Phase.READY
    assert result.agent_session_id == h.host.agent_session_id
    assert result.resumed is False and result.replayed == "none"
    assert result.acp.protocol_version == 1
    assert result.acp.agent_capabilities["loadSession"] is True
    assert result.bridge.instance == "b-1"
    # server 给的覆盖兜底值，没给的沿用兜底
    assert h.host.limits.deadline_s == 900 and h.host.limits.idle_s == 120


async def test_open_twice_and_turn_before_open_are_rejected(make: Any) -> None:
    h = make()
    await h.host.boot()
    with pytest.raises(SessionNotOpen) as info:
        await h.start()
    assert info.value.cause == "not_opened"
    await h.host.open(OpenParams())
    with pytest.raises(SessionAlreadyOpen):
        await h.host.open(OpenParams())


async def test_resume_with_full_replay_sends_history_before_responding(make: Any) -> None:
    h = make()
    h.agent.fake.history["old-1"] = ["q1"]
    await h.host.boot()
    result = await h.host.open(
        OpenParams(resume=ResumeSpec(agent_session_id="old-1", replay="full"))
    )
    assert (result.agent_session_id, result.resumed, result.replayed) == ("old-1", True, "full")
    replays = h.outbox.of("agent.update")
    assert [u["origin"] for u in replays] == ["replay", "replay"]  # 响应之前已全部放入
    assert all("turnId" not in u for u in replays)
    assert h.agent_calls("session/load")


async def test_resume_without_replay_uses_session_resume(make: Any) -> None:
    h = make()
    h.agent.fake.history["old-1"] = ["q1"]
    await h.host.boot()
    result = await h.host.open(OpenParams(resume=ResumeSpec(agent_session_id="old-1")))
    assert (result.resumed, result.replayed) == (True, "none")
    assert h.agent_calls("session/resume") and not h.agent_calls("session/load")
    assert h.outbox.of("agent.update") == []


async def test_resume_of_unknown_session_falls_back_to_new_and_says_so(make: Any) -> None:
    """★ 恢复失败不是错误，但上下文丢失必须如实报告（§5.6）。"""
    h = make()
    await h.host.boot()
    result = await h.host.open(
        OpenParams(resume=ResumeSpec(agent_session_id="gone", replay="full"))
    )
    assert result.resumed is False and result.replayed == "none"
    assert result.agent_session_id != "gone"
    assert h.host.phase is Phase.READY


async def test_background_updates_are_stray(make: Any) -> None:
    h = make(Script(background_updates=2))
    await h.open()
    await until(lambda: len(h.outbox.of("agent.update")) == 2)
    assert {u["origin"] for u in h.outbox.of("agent.update")} == {"stray"}


# ═══════════════════════════════════ open 失败 ═══════════════════════════════════


async def test_open_timeout_reboots_the_agent_and_open_can_be_retried(make: Any) -> None:
    """★ open 超时时 agent 可能仍在处理（比如还在重放）：重启它回到 IDLE，
    server 可以重试（§6.5）。"""
    h = make(Script(on_open="silent"), open_timeout_s=0.1)
    h.agent.script_after_restart = Script()
    await h.host.boot()
    with pytest.raises(AcpRequestTimeout):
        await h.host.open(OpenParams())
    await until(lambda: h.agent.restarts == 1 and h.host.phase is Phase.IDLE)
    await h.host.open(OpenParams())
    assert h.host.phase is Phase.READY
    assert h.outbox.of("session.state") == []  # 还没有会话，没什么可报告的


async def test_agent_exit_during_open_is_agent_unavailable_and_reboots(make: Any) -> None:
    h = make(Script(on_open="crash"))
    h.agent.script_after_restart = Script()
    await h.host.boot()
    with pytest.raises(AgentUnavailable) as info:
        await h.host.open(OpenParams())
    assert info.value.cause == "agent_exited"
    await until(lambda: h.agent.restarts == 1 and h.host.phase is Phase.IDLE)
    await h.host.open(OpenParams())
    assert h.host.phase is Phase.READY


async def test_close_during_open_never_reaches_ready(make: Any) -> None:
    """open 与 close 都要 await，会交错：close 赢，open 失败，会话停在 ENDED。"""
    h = make(Script(on_open="silent"))
    await h.host.boot()
    opening = asyncio.create_task(h.host.open(OpenParams()))
    await until(lambda: h.host.phase is Phase.OPENING and bool(h.agent_calls("session/new")))
    await asyncio.wait_for(h.host.close(), 2)
    with pytest.raises(BridgeError):
        await asyncio.wait_for(opening, 2)
    assert h.host.phase is Phase.ENDED and h.host.ended.is_set()
    assert h.host.agent_session_id is None
    assert h.agent.restarts == 0  # 关闭中的退出是预期内的，不重启
    assert h.outbox.of("session.state") == [] and h.outbox.of("session.ended") == []


# ═══════════════════════════════════ 一轮 ═══════════════════════════════════


async def test_a_full_turn(make: Any) -> None:
    h = make()
    await h.open()
    await h.start(trace="00-abc-def-01")
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "completed", "stopReason": "end_turn"}
    assert ended["response"] == {"stopReason": "end_turn"}  # prompt 响应原样转交（§3 D2）
    assert h.host.phase is Phase.READY and h.host.turn is None

    sequence = [(m.method, m.wire.get("state") or m.wire.get("origin")) for m in h.outbox.messages]
    assert sequence == [
        ("turn.state", "running"),
        ("agent.update", "turn"),
        ("agent.update", "turn"),
        ("turn.state", "ended"),  # 一定排在这一轮全部 update 之后
    ]
    seqs = [m.seq for m in h.outbox.messages]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert [m.kind for m in h.outbox.messages] == ["control", "data", "data", "control"]

    update = h.outbox.of("agent.update")[0]
    assert update["turnId"] == "t1"
    assert update["update"] == {  # ★ B2：ACP 的 params 一字不改
        "sessionId": h.host.agent_session_id,
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "第 1 段"},
        },
    }
    assert h.agent_calls("session/prompt")[0]["_meta"] == {"traceparent": "00-abc-def-01"}


async def test_turn_start_is_idempotent(make: Any) -> None:
    """★ 同一个 turnId 重复提交不会开出第二轮（§4.6）。"""
    h = make(Script(on_prompt="silent"))
    await h.open()
    await h.start("t1")
    await h.start("t1")  # 进行中：已接纳
    await until(lambda: len(h.agent_calls("session/prompt")) == 1)
    with pytest.raises(TurnBusy):
        await h.start("t2")
    h.host.cancel_turn("t1")
    await h.turn_over()
    await h.start("t1")  # 已结束：已接纳，并重发一次 ended
    assert h.outbox.turn_states("t1").count("ended") == 2
    assert len(h.agent_calls("session/prompt")) == 1


async def test_next_turn_after_the_previous_one(make: Any) -> None:
    h = make()
    await h.open()
    await h.start("t1")
    await h.turn_over("t1")
    await h.start("t2")
    assert (await h.turn_over("t2"))["outcome"]["kind"] == "completed"


# ═══════════════════════════════════ 取消 ═══════════════════════════════════


async def test_cancel_requested(make: Any) -> None:
    h = make(Script(on_prompt="silent"))
    await h.open()
    await h.start()
    assert h.host.cancel_turn("t1").state == "cancelling"
    assert h.host.cancel_turn("t1").state == "cancelling"  # 幂等
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "cancelled", "cause": "requested"}
    assert h.outbox.turn_states("t1") == ["running", "cancelling", "ended"]
    assert h.host.cancel_turn("t1").state == "ended"
    with pytest.raises(TurnNotFound):
        h.host.cancel_turn("nope")


async def test_cancel_racing_with_completion_reports_completed(make: Any) -> None:
    """★ 取消时 agent 恰好做完：以 agent 的实际结果为准（§6.4）。"""
    h = make(Script(on_prompt="silent", on_cancel="finish"))
    await h.open()
    await h.start()
    h.host.cancel_turn("t1")
    assert (await h.turn_over())["outcome"] == {"kind": "completed", "stopReason": "end_turn"}


async def test_error_on_cancel_still_counts_as_cancelled(make: Any) -> None:
    h = make(Script(on_prompt="silent", on_cancel="error"))
    await h.open()
    await h.start()
    h.host.cancel_turn("t1")
    assert (await h.turn_over())["outcome"] == {"kind": "cancelled", "cause": "requested"}


async def test_by_default_a_turn_has_no_deadline(make: Any) -> None:
    """★ CLI 在干活就不中断：默认没有截止，只有用户取消或 CLI 自己结束。"""
    h = make(Script(on_prompt="silent"))
    await h.open(defaults=TurnLimits(idle_s=10**9, tool_idle_s=10**9))  # 隔离静默检测
    await h.start()
    h.clock.advance(10 * 86_400)
    assert h.outbox.turn_states("t1") == ["running"]
    h.host.cancel_turn("t1")
    assert (await h.turn_over())["outcome"] == {"kind": "cancelled", "cause": "requested"}


async def test_by_default_a_disconnect_never_cancels_the_turn(make: Any) -> None:
    """★ 断线只是通道断了：默认没有重连窗口，这一轮照常进行，等 server 回来 attach。"""
    h = make(Script(on_prompt="silent"))
    await h.open(defaults=TurnLimits(idle_s=10**9, tool_idle_s=10**9))
    h.host.upstream_changed(True)
    await h.start()
    h.host.upstream_changed(False)
    h.clock.advance(10 * 86_400)
    assert h.outbox.turn_states("t1") == ["running"]
    h.host.upstream_changed(True)
    h.host.cancel_turn("t1")
    assert (await h.turn_over())["outcome"]["cause"] == "requested"


async def test_deadline(make: Any) -> None:
    """server 显式给了 deadlineS 时才有截止（默认不给）。"""
    h = make(Script(on_prompt="silent"))
    await h.open()
    await h.start(limits=TurnLimits(deadline_s=10))
    h.clock.advance(9.9)
    assert h.outbox.turn_states("t1") == ["running"]
    h.clock.advance(0.1)
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "cancelled", "cause": "deadline"}
    assert ended["stats"]["elapsedS"] == 10


async def test_idle_uses_the_tool_threshold_while_a_tool_runs(make: Any) -> None:
    """★ 长命令没有输出不算卡死：有工具在执行时静默阈值切到 toolIdleS（§6.2）。"""
    h = make(Script(on_prompt="silent", updates_per_turn=0))
    await h.open(defaults=TurnLimits(idle_s=5, tool_idle_s=50))
    await h.start()
    sid = h.host.agent_session_id
    running = {"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "in_progress"}
    await h.host.on_agent_update({"sessionId": sid, "update": running})
    h.clock.advance(49)
    assert h.outbox.turn_states("t1") == ["running"]
    done = {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    await h.host.on_agent_update({"sessionId": sid, "update": done})
    h.clock.advance(4.9)  # 工具结束，回到 idleS，从这条 update 起重新计时
    assert h.outbox.turn_states("t1") == ["running"]
    h.clock.advance(0.1)
    assert (await h.turn_over())["outcome"] == {"kind": "cancelled", "cause": "idle"}


async def test_cancel_unanswered_kills_the_agent(make: Any) -> None:
    """宽限到点仍无结果 → failed / cancel_unanswered，终止 agent（§6.4 ⑥）。"""
    h = make(Script(on_prompt="silent", on_cancel="ignore"))
    await h.open()
    await h.start()
    h.host.cancel_turn("t1")
    h.clock.advance(14.9)
    assert "t1" not in h.host.ledger
    h.clock.advance(0.1)
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "failed", "cause": "cancel_unanswered"}
    # 终止并重启 agent，恢复同一个会话（详见 test_recovery.py）
    await until(lambda: h.host.phase is Phase.READY)
    assert h.agent.restarts == 1
    assert h.outbox.of("session.state")[0]["agent"]["cause"] == "cancel_unanswered"


# ═══════════════════════════════════ agent 退出 ═══════════════════════════════════


async def test_agent_crash_mid_turn(make: Any) -> None:
    h = make(Script(on_prompt="crash"))
    await h.open()
    await h.start()
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "failed", "cause": "agent_exited"}
    await until(lambda: h.host.phase is Phase.READY)
    # 结束只宣告一次（B1）：退出监视与 prompt 失败都会报告，但只有一个 ended
    assert h.outbox.turn_states("t1").count("ended") == 1


# ═══════════════════════════════════ agent 报错 ═══════════════════════════════════


async def test_prompt_error_fails_the_turn_without_restarting_the_agent(make: Any) -> None:
    """agent 回了错误 ≠ 进程坏了：这一轮 agent_error，会话照常可用。"""
    h = make(Script(on_prompt="error"))
    await h.open()
    await h.start()
    ended = await h.turn_over()
    assert ended["outcome"]["kind"] == "failed"
    assert ended["outcome"]["cause"] == "agent_error"
    assert ended["outcome"]["error"]["acp"]["message"] == "fake agent 出错了"
    assert h.host.phase is Phase.READY
    assert h.agent.restarts == 0 and h.outbox.of("session.state") == []

    h.agent.fake.script = Script()
    await h.start("t2")
    assert (await h.turn_over("t2"))["outcome"]["kind"] == "completed"


async def test_prompt_response_without_stop_reason_is_an_agent_error(make: Any) -> None:
    """违反规范的响应（适配器升级后最可能出现的偏差）：判失败，并说明原因。"""
    h = make(Script(on_prompt="no_stop_reason"))
    await h.open()
    await h.start()
    ended = await h.turn_over()
    assert ended["outcome"]["kind"] == "failed"
    assert ended["outcome"]["cause"] == "agent_error"
    assert ended["outcome"]["error"]["cause"] == "invalid_response"
    assert h.host.phase is Phase.READY and h.agent.restarts == 0


# ═══════════════════════════════════ close ═══════════════════════════════════


async def test_close_cancels_the_running_turn_first(make: Any) -> None:
    h = make(Script(on_prompt="silent"))
    await h.open()
    await h.start()
    await asyncio.wait_for(h.host.close(), 2)
    assert h.ended()["outcome"] == {"kind": "cancelled", "cause": "session_closing"}
    assert h.agent_calls("session/close")
    assert h.host.phase is Phase.ENDED and h.host.ended.is_set()
    assert h.agent.shutdowns == 1
    assert h.outbox.of("session.ended") == []  # server 要求的关闭不发 session.ended
    await asyncio.wait_for(h.host.close(), 1)  # 幂等


async def test_close_without_open(make: Any) -> None:
    h = make()
    await h.host.boot()
    await asyncio.wait_for(h.host.close(), 2)
    assert h.host.phase is Phase.ENDED
    assert not h.agent_calls("session/close")
