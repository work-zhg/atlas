"""CLI 运行时的已知缺陷（bench/doc/cli-func-cases.html §3），现在预期失败（xfail strict）。

修好以后对应的用例会 XPASS 而报错：提醒把它转成常规用例，并去掉评测任务里的 known_defect
（bench/agent-tasks/cli-eng 的 hnd-*、prm-manual-deny、prm-plan）。

接管的做法同生产：持有 run 的实例没了（这里是取消驱动它的任务，连接随之关闭），这一轮在 bridge 里
照常等着；另一个实例拿同一个 run id、按 cli_session 里的 bridge 实例和确认点 attach 接上。
确认点每 1 秒写一次（cli/link.py 的 _ACK_INTERVAL_S）：改成 0 = 流出的内容都已确认，
改成很大 = 只确认了第一条，用来分别卡住两种时机，不靠睡眠。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_agent.cli import link as link_mod
from atlas_agent.errors import InvalidApprovalMode
from atlas_agent.services.run import _approval_mode_for
from atlas_bridge.testing.fake_agent import Script
from atlas_engine.contracts.events import EventType, TraceEvent

# 夹具 env 按定义时的名字注册，起个别名导入只是为了不和参数重名
from tests.test_cli_runtime import Env, prepared
from tests.test_cli_runtime import env as _cli_env  # noqa: F401

#: FakeAgent 这一轮流出的正文（两段 update，在权限请求之前）
ANSWER = "第 1 段第 2 段"


def _text(events: list[TraceEvent]) -> str:
    """最后一条 message.completed 的正文（落库的回答）。"""
    done = [e for e in events if e.type is EventType.MESSAGE_COMPLETED]
    content = done[-1].data["content"] if done else []
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


async def _collect(e: Env, segment: Any, run_id: UUID, out: list[TraceEvent]) -> None:
    async for event in e.runtime.run_turn(segment, run_id=run_id, redis=None, relay=e.relay):
        out.append(event)


async def _until(events: list[TraceEvent], kind: EventType, timeout: float = 10.0) -> None:
    async def wait() -> None:
        while not any(x.type is kind for x in events):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), timeout)


async def _takeover(e: Env) -> tuple[list[TraceEvent], list[TraceEvent]]:
    """一轮停在等审批时实例没了，另一个实例接管同一个 run；接管后有人批准。

    → (旧实例发出的事件, 接手实例发出的事件)。
    """
    e.approvals.after_polls = 10_000  # 先没人答复：这一轮停在等审批
    run_id = uuid4()
    first: list[TraceEvent] = []
    old = asyncio.create_task(_collect(e, prepared(e.thread), run_id, first))
    await _until(first, EventType.APPROVAL_REQUIRED)
    old.cancel()  # 持有 run 的实例没了：连接断开，bridge 里这一轮照常等着
    with contextlib.suppress(asyncio.CancelledError):
        await old

    segment = prepared(
        e.thread,
        cli_session_id=e.sessions.remembered[-1][1],
        cli_bridge_instance=e.sessions.instance,
        cli_acked_seq=e.sessions.acked_seq,
    )
    segment.takeover = SimpleNamespace(reason="expired")
    segment.start_seq = max(x.seq for x in first)
    second: list[TraceEvent] = []
    new = asyncio.create_task(_collect(e, segment, run_id, second))
    # bridge 以同一个 id 补发还没答复的询问：等它到了再批准
    await asyncio.sleep(1.0)
    e.approvals.after_polls = 0
    await asyncio.wait_for(new, 20)
    return first, second


def _count(events: list[TraceEvent], kind: EventType, **where: Any) -> int:
    return sum(
        1 for x in events if x.type is kind and all(x.data.get(k) == v for k, v in where.items())
    )


@pytest.mark.xfail(strict=True, reason="CD3 接手实例的翻译器从空开始，落库的回答缺接管前流出的段落")
async def test_cd3_the_answer_saved_after_a_takeover_keeps_what_was_said_before(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_mod, "_ACK_INTERVAL_S", 0.0)  # 流出的内容都已确认：接管后不再补发
    e = await env(Script(ask_permission=True))
    first, second = await _takeover(e)
    assert _count(first, EventType.MESSAGE_DELTA) == 2  # 前提：接管前正文已经流出
    assert second[-1].type is EventType.RUN_FINISHED
    assert ANSWER in _text(second)


@pytest.mark.xfail(strict=True, reason="CD4 确认点之后、旧实例已发布的消息，接管后又发布一遍")
async def test_cd4_events_published_before_a_takeover_are_not_published_again(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(link_mod, "_ACK_INTERVAL_S", 3600.0)  # 只确认了第一条
    e = await env(Script(ask_permission=True))
    first, second = await _takeover(e)
    assert second[-1].type is EventType.RUN_FINISHED
    both = first + second
    assert _count(both, EventType.TOOL_STARTED, call_id="call-1") == 1
    assert _count(both, EventType.MESSAGE_DELTA) == 2


@pytest.mark.xfail(strict=True, reason="CD6 接手实例不记得已经发过，approval.required 再发一次")
async def test_cd6_a_takeover_does_not_raise_the_same_approval_again(env: Any) -> None:
    e = await env(Script(ask_permission=True))
    first, second = await _takeover(e)
    assert second[-1].type is EventType.RUN_FINISHED
    assert len(e.approvals.polls) == 1  # 前提：始终是同一条审批（id 由 run 和调用派生）
    assert _count(first + second, EventType.APPROVAL_REQUIRED) == 1


@pytest.mark.xfail(strict=True, reason="CD10 被拒的调用两条终态：平台挡下一条，CLI 终态一条")
async def test_cd10_a_rejected_call_has_exactly_one_terminal_event(env: Any) -> None:
    e = await env(Script(ask_permission=True))
    e.approvals.decision = "rejected"
    events = await e.run()
    assert events[-1].type is EventType.RUN_FINISHED
    assert _count(events, EventType.TOOL_FAILED, result="用户拒绝了这次调用。") == 1  # 前提
    terminal = [x for x in events if x.type in (EventType.TOOL_FAILED, EventType.TOOL_COMPLETED)]
    assert len(terminal) == 1


def test_prm08_a_cli_agent_takes_no_approval_mode() -> None:
    """PRM-08：给 CLI 智能体发消息时带 approval_mode → 400（权限由 CLI 的权限模式决定）。"""
    spec = {
        "kind": "cli",
        "system_prompt": "x",
        "model": {"model": "m", "provider": "anthropic"},
        "cli": {"cli_type": "claude-code", "adapter": "a", "image": "i"},
    }
    assert _approval_mode_for(spec, None, None) is None
    assert _approval_mode_for(spec, "manual", None) is None  # 会话记住的旧值不报错，照样不用
    with pytest.raises(InvalidApprovalMode) as raised:
        _approval_mode_for(spec, None, "manual")
    assert raised.value.status_code == 400
