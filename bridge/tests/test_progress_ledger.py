"""ProgressTracker 与 TurnLedger。"""

from __future__ import annotations

from typing import Any

from atlas_bridge.session.ledger import TurnLedger, TurnRecord
from atlas_bridge.session.progress import ProgressTracker
from atlas_host import Completed, TurnStats


def tool(call_id: str, status: str | None, kind: str = "tool_call_update") -> dict[str, Any]:
    update: dict[str, Any] = {"sessionUpdate": kind, "toolCallId": call_id}
    if status is not None:
        update["status"] = status
    return {"sessionId": "s", "update": update}


def test_threshold_follows_tools_in_progress() -> None:
    p = ProgressTracker(idle_s=120, tool_idle_s=600)
    assert p.idle_threshold == 120
    p.observe(tool("a", "pending", kind="tool_call"))
    # ★ 出现了、没到终态就算在执行：适配器可能从 pending 直接跳到 completed
    assert p.idle_threshold == 600
    p.observe(tool("a", "in_progress"))
    p.observe(tool("b", "in_progress", kind="tool_call"))
    assert p.idle_threshold == 600
    p.observe(tool("a", None))  # 只更新了内容，没带状态
    p.observe(tool("a", "completed"))
    assert p.idle_threshold == 600  # b 还在执行
    p.observe(tool("b", "failed"))
    assert p.idle_threshold == 120


def test_non_tool_and_malformed_updates_are_ignored() -> None:
    p = ProgressTracker(idle_s=1, tool_idle_s=2)
    for raw in ({"update": {"sessionUpdate": "agent_message_chunk"}}, {}, {"update": 3}):
        p.observe(raw)  # type: ignore[arg-type]
    assert p.in_progress_tools == set()


def test_ledger_keeps_most_recent() -> None:
    ledger = TurnLedger(size=2)
    rec = TurnRecord(
        Completed(stop_reason="end_turn"), TurnStats(elapsed_s=1, awaiting_permission_s=0)
    )
    for turn_id in ("t1", "t2", "t3"):
        ledger.record(turn_id, rec)
    assert "t1" not in ledger and "t2" in ledger and "t3" in ledger
    assert ledger.get("t3") is rec and len(ledger) == 2
