"""PermissionBroker：询问的上报、答复、过期、撤回与兜底（Bridge 设计 §4.7 · §6.3 · §6.4）。

★ 贯穿全部用例的两条：绝不自动批准（B3）；agent 的请求永远有回应（B6）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from atlas_bridge.session.permissions import reject_result
from atlas_bridge.testing.fake_agent import Script
from atlas_bridge.testing.harness import SessionHarness, until
from atlas_host import TurnLimits

ASKING = Script(ask_permission=True)


async def asked(h: SessionHarness, n: int = 1) -> list[Any]:
    """等到第 n 个 permission.ask 放入 Outbox，返回全部询问的 (request_id, params)。"""
    await until(lambda: len(h.outbox.of("permission.ask")) >= n)
    return [(m.request_id, m.wire) for m in h.outbox.messages if m.method == "permission.ask"]


def tool_result(h: SessionHarness) -> str:
    """agent 收到答复后报告的工具状态：completed = 被允许，failed = 被拒绝。"""
    last = h.outbox.of("agent.update")[-1]["update"]["update"]
    assert last["sessionUpdate"] == "tool_call_update"
    return last["status"]


# ═══════════════════════════════════ 正常往返 ═══════════════════════════════════


async def test_ask_goes_up_and_the_chosen_option_goes_back(make: Any) -> None:
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, params)] = await asked(h)
    assert params["turnId"] == "t1"
    assert params["request"]["options"][0] == {  # ★ ACP 的 params 原样（B2）
        "optionId": "allow",
        "name": "允许",
        "kind": "allow_once",
    }
    assert "expiresInS" not in params and "expiresAt" not in params  # 默认不过期
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission"]

    h.host.on_permission_answer(ask_id, {"optionId": "allow"})
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    assert tool_result(h) == "completed"
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission", "running", "ended"]
    assert h.outbox.of("permission.withdraw") == []  # server 答复的，不撤回


@pytest.mark.parametrize(
    "answer",
    [
        {"reject": True},
        {"optionId": "no-such-option"},  # 不在 agent 的选项里 → 拒绝
        {"optionId": "allow", "reject": True},  # 形状不对 → 拒绝
        "garbage",
    ],
)
async def test_anything_but_a_valid_choice_is_a_rejection(make: Any, answer: Any) -> None:
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.host.on_permission_answer(ask_id, answer)
    await h.turn_over()
    assert tool_result(h) == "failed"


async def test_server_error_is_a_rejection(make: Any) -> None:
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.host.on_permission_answer(ask_id, error={"code": -32603, "message": "boom"})
    await h.turn_over()
    assert tool_result(h) == "failed"


async def test_late_and_unknown_answers_are_ignored(make: Any) -> None:
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.host.on_permission_answer(ask_id, {"reject": True})
    h.host.on_permission_answer(ask_id, {"optionId": "allow"})  # 已有结果：忽略
    h.host.on_permission_answer(999, {"optionId": "allow"})
    await h.turn_over()
    assert tool_result(h) == "failed"


def test_reject_result_prefers_reject_once_then_reject_always_then_cancelled() -> None:
    def opts(*kinds: str) -> dict[str, Any]:
        return {"options": [{"optionId": k, "kind": k} for k in kinds]}

    assert reject_result(opts("allow_once", "reject_always", "reject_once"))["outcome"] == {
        "outcome": "selected",
        "optionId": "reject_once",
    }
    assert reject_result(opts("reject_always"))["outcome"]["optionId"] == "reject_always"
    # agent 没提供任何拒绝选项：只能回 cancelled，绝不选 allow
    assert reject_result(opts("allow_once", "allow_always")) == {
        "outcome": {"outcome": "cancelled"}
    }
    assert reject_result("garbage") == {"outcome": {"outcome": "cancelled"}}


# ═══════════════════════════════════ 计时 ═══════════════════════════════════


async def test_idle_is_paused_while_waiting_for_a_human(make: Any) -> None:
    """★ 人在想不等于 agent 卡死（§6.2）。答复后静默计时从头开始。"""
    h = make(Script(ask_permission=True, updates_per_turn=0))
    await h.open(defaults=TurnLimits(idle_s=5, permission_wait_s=1000))
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.clock.advance(500)
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission"]
    h.host.on_permission_answer(ask_id, {"optionId": "allow"})
    ended = await h.turn_over()
    assert ended["stats"]["awaitingPermissionS"] == 500


async def test_by_default_an_ask_waits_for_a_human_however_long_it_takes(make: Any) -> None:
    """★ 审批只由人决定：默认没有过期，十天后答复照样生效，期间不撤回、不替用户拒绝。"""
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.clock.advance(10 * 86_400)
    assert h.outbox.of("permission.withdraw") == []
    assert h.outbox.turn_states("t1") == ["running", "awaiting_permission"]
    h.host.on_permission_answer(ask_id, {"optionId": "allow"})
    assert (await h.turn_over())["outcome"]["kind"] == "completed"
    assert tool_result(h) == "completed"


async def test_expired_ask_is_rejected_and_withdrawn(make: Any) -> None:
    """server 显式给了 permissionWaitS 时才会过期（默认不给）。"""
    h = make(ASKING)
    await h.open(defaults=TurnLimits(permission_wait_s=60))
    await h.start()
    [(ask_id, params)] = await asked(h)
    assert params["expiresInS"] == 60
    h.clock.advance(60)
    await h.turn_over()
    assert h.outbox.of("permission.withdraw") == [
        {"seq": h.outbox.of("permission.withdraw")[0]["seq"], "askId": ask_id, "reason": "expired"}
    ]
    assert tool_result(h) == "failed"  # 给 agent 的是拒绝，不是批准
    assert h.ended()["outcome"]["kind"] == "completed"  # 轮次回到 running 后正常结束


async def test_deadline_before_expiry_cancels_the_turn(make: Any) -> None:
    """询问的过期时刻 = min(permissionWaitS, 截止)。截止先到：随这一轮一起取消（§6.3）。"""
    h = make(ASKING)
    await h.open(defaults=TurnLimits(permission_wait_s=600, deadline_s=100))
    await h.start()
    [(_, params)] = await asked(h)
    assert params["expiresInS"] == 100
    h.clock.advance(100)
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "cancelled", "cause": "deadline"}
    assert [w["reason"] for w in h.outbox.of("permission.withdraw")] == ["turn_ended"]


# ═══════════════════════════════════ 取消与兜底 ═══════════════════════════════════


async def test_cancel_answers_pending_asks_with_cancelled(make: Any) -> None:
    """★ ACP 的取消义务：挂起的询问全部回 cancelled，并向 server 撤回（§6.4 ②）。"""
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.clock.advance(30)
    h.host.cancel_turn("t1")
    ended = await h.turn_over()
    # FakeAgent 收到 cancelled 后以 cancelled 结束这一轮
    assert ended["outcome"] == {"kind": "cancelled", "cause": "requested"}
    assert ended["stats"]["awaitingPermissionS"] == 30
    assert [(w["askId"], w["reason"]) for w in h.outbox.of("permission.withdraw")] == [
        (ask_id, "turn_ended")
    ]
    methods = [m.method for m in h.outbox.messages]
    assert methods.index("permission.withdraw") < len(methods) - 1  # 撤回排在 ended 之前
    h.host.on_permission_answer(ask_id, {"optionId": "allow"})  # 撤回之后的答复：忽略


async def test_ask_during_cancelling_is_not_reported(make: Any) -> None:
    h = make(Script(on_prompt="silent", on_cancel="ignore"))
    await h.open()
    await h.start()
    h.host.cancel_turn("t1")
    result = await h.host.on_permission_request(
        {"options": [{"optionId": "a", "kind": "allow_once"}]}
    )
    assert result == {"outcome": {"outcome": "cancelled"}}
    assert h.outbox.of("permission.ask") == []


async def test_ask_without_a_turn_is_rejected(make: Any) -> None:
    h = make()
    await h.open()
    request = {"options": [{"optionId": "no", "kind": "reject_once"}]}
    assert (await h.host.on_permission_request(request))["outcome"]["optionId"] == "no"
    assert h.outbox.of("permission.ask") == []


async def test_too_many_pending_asks_are_rejected(make: Any) -> None:
    h = make(Script(on_prompt="silent"), max_pending_asks=2)
    await h.open()
    await h.start()
    request = {
        "options": [
            {"optionId": "y", "kind": "allow_once"},
            {"optionId": "n", "kind": "reject_once"},
        ]
    }
    tasks = [asyncio.create_task(h.host.on_permission_request(request)) for _ in range(2)]
    await asked(h, 2)
    third = await asyncio.wait_for(h.host.on_permission_request(request), 1)
    assert third["outcome"]["optionId"] == "n"
    assert len(h.outbox.of("permission.ask")) == 2
    h.host.cancel_turn("t1")
    results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert results == [{"outcome": {"outcome": "cancelled"}}] * 2


async def test_agent_exit_while_asking_withdraws_once(make: Any) -> None:
    h = make(ASKING)
    await h.open()
    await h.start()
    [(ask_id, _)] = await asked(h)
    h.agent.crash()
    ended = await h.turn_over()
    assert ended["outcome"] == {"kind": "failed", "cause": "agent_exited"}
    await until(lambda: len(h.outbox.of("permission.withdraw")) >= 1)
    await asyncio.sleep(0.01)
    assert [w["askId"] for w in h.outbox.of("permission.withdraw")] == [ask_id]
    assert h.host.permissions.asks == {}
