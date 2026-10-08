"""上游协议消息模型（Bridge 设计 §4）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from atlas_host import (
    DEFAULT_LIMITS,
    AgentUpdateParams,
    AttachResult,
    CancelCause,
    Cancelled,
    CloseCode,
    Completed,
    FailCause,
    Failed,
    OpenParams,
    OpenResult,
    PermissionAnswer,
    PermissionAskParams,
    SeqRange,
    TurnLimits,
    TurnStartParams,
    TurnState,
    TurnStateParams,
    methods,
    should_reconnect,
)
from pydantic import ValidationError

# ─────────────────────────── 线上格式 ───────────────────────────


def test_wire_format_is_camel_case_and_omits_none() -> None:
    params = TurnStartParams(turn_id="r1", prompt=[{"type": "text", "text": "hi"}])
    assert params.to_wire() == {"turnId": "r1", "prompt": [{"type": "text", "text": "hi"}]}


def test_parses_camel_case_from_the_wire() -> None:
    params = OpenParams.model_validate(
        {
            "resume": {"agentSessionId": "s1", "replay": "full"},
            "mcpServers": [{"type": "http", "name": "serpapi", "url": "http://x", "headers": []}],
            "defaults": {"deadlineS": 900},
        }
    )
    assert params.resume is not None and params.resume.replay == "full"
    assert params.defaults.deadline_s == 900
    assert params.mcp_servers[0]["name"] == "serpapi"


def test_unknown_fields_are_ignored_for_forward_compatibility() -> None:
    """★ 新版本一方带来的可选字段，旧版本一方要能接受（§4.3）。"""
    params = TurnStartParams.model_validate(
        {"turnId": "r1", "prompt": [{"type": "text", "text": "x"}], "someFutureField": 1}
    )
    assert params.turn_id == "r1"


def test_acp_content_passes_through_unchanged() -> None:
    """★ ACP 的内容对上游协议是不透明的，经过模型后必须一字不差（§3.8 B2）。"""
    update = {
        "sessionId": "s1",
        "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "rawInput": {"a": [1, None]}},
        "_meta": {"traceparent": "00-abc"},
    }
    msg = AgentUpdateParams(seq=1, origin="turn", turn_id="r1", update=update)
    assert AgentUpdateParams.model_validate(msg.to_wire()).update == update


def test_open_result_roundtrip() -> None:
    result = OpenResult.model_validate(
        {
            "agentSessionId": "s1",
            "resumed": False,
            "replayed": "none",
            "acp": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
            "bridge": {"version": "2.0.0", "instance": "b-1"},
        }
    )
    assert OpenResult.model_validate(result.to_wire()) == result
    assert result.features == []


# ─────────────────────────── 不变式 ───────────────────────────


def test_turn_state_outcome_iff_ended() -> None:
    TurnStateParams(seq=1, turn_id="r1", state=TurnState.RUNNING)
    TurnStateParams(
        seq=2, turn_id="r1", state=TurnState.ENDED, outcome=Completed(stop_reason="end_turn")
    )
    with pytest.raises(ValidationError):
        TurnStateParams(seq=3, turn_id="r1", state=TurnState.ENDED)
    with pytest.raises(ValidationError):
        TurnStateParams(
            seq=4,
            turn_id="r1",
            state=TurnState.RUNNING,
            outcome=Cancelled(cause=CancelCause.IDLE),
        )


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ({"kind": "completed", "stopReason": "max_tokens"}, Completed(stop_reason="max_tokens")),
        ({"kind": "cancelled", "cause": "deadline"}, Cancelled(cause=CancelCause.DEADLINE)),
        (
            {"kind": "failed", "cause": "cancel_unanswered"},
            Failed(cause=FailCause.CANCEL_UNANSWERED),
        ),
    ],
)
def test_outcome_is_discriminated_by_kind(wire: dict, expected: object) -> None:
    msg = TurnStateParams.model_validate(
        {"seq": 9, "turnId": "r1", "state": "ended", "outcome": wire}
    )
    assert msg.outcome == expected


def test_unknown_cancel_cause_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Cancelled.model_validate({"kind": "cancelled", "cause": "because"})


def test_agent_update_turn_id_iff_origin_turn() -> None:
    AgentUpdateParams(seq=1, origin="replay", update={})
    AgentUpdateParams(seq=2, origin="stray", update={})
    with pytest.raises(ValidationError):
        AgentUpdateParams(seq=3, origin="turn", update={})
    with pytest.raises(ValidationError):
        AgentUpdateParams(seq=4, origin="replay", turn_id="r1", update={})


@pytest.mark.parametrize(
    ("wire", "ok"),
    [
        ({"optionId": "a1"}, True),
        ({"reject": True}, True),
        ({}, False),
        ({"reject": False}, False),
        ({"optionId": "a1", "reject": True}, False),
    ],
)
def test_permission_answer_is_exactly_one_of(wire: dict, ok: bool) -> None:
    if ok:
        PermissionAnswer.model_validate(wire)
    else:
        with pytest.raises(ValidationError):
            PermissionAnswer.model_validate(wire)


def test_seq_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AgentUpdateParams(seq=0, origin="stray", update={})


def test_seq_range_must_be_ordered() -> None:
    SeqRange(first=3, last=3)
    with pytest.raises(ValidationError):
        SeqRange(first=5, last=4)


def test_attach_result_with_gaps() -> None:
    result = AttachResult.model_validate(
        {
            "state": "in_turn",
            "turn": {"turnId": "r1", "state": "awaiting_permission"},
            "resendFrom": 413,
            "gaps": [{"first": 300, "last": 350}],
        }
    )
    assert result.gaps[0].last == 350 and result.turn is not None


def test_turn_start_requires_non_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        TurnStartParams(turn_id="r1", prompt=[])


def test_permission_ask_carries_both_absolute_and_relative_expiry() -> None:
    ask = PermissionAskParams(
        seq=5,
        turn_id="r1",
        request={"sessionId": "s", "options": []},
        expires_at=datetime(2026, 9, 30, 10, 12, tzinfo=UTC),
        expires_in_s=600,
    )
    wire = ask.to_wire()
    assert wire["expiresInS"] == 600 and wire["expiresAt"].startswith("2026-09-30T10:12")


def test_permission_ask_without_expiry_waits_for_a_human() -> None:
    """★ 默认不过期：审批只由人决定，线上不出现过期字段。"""
    ask = PermissionAskParams(seq=5, turn_id="r1", request={"sessionId": "s", "options": []})
    wire = ask.to_wire()
    assert "expiresInS" not in wire and "expiresAt" not in wire


# ─────────────────────────── 时限 ───────────────────────────


def test_limits_override_only_given_fields() -> None:
    session = TurnLimits(deadline_s=900).over(DEFAULT_LIMITS)
    turn = TurnLimits(idle_s=60).over(session)
    assert (turn.idle_s, turn.deadline_s, turn.tool_idle_s) == (60, 900, 600)
    assert turn.is_complete()


def test_default_limits_are_complete() -> None:
    assert DEFAULT_LIMITS.is_complete()


def test_deadline_permission_wait_and_reconnect_window_default_to_unlimited() -> None:
    """★ CLI 在干活就不中断、审批只由人决定、断线不结束这一轮。"""
    assert DEFAULT_LIMITS.deadline_s is None
    assert DEFAULT_LIMITS.permission_wait_s is None
    assert DEFAULT_LIMITS.reconnect_window_s is None
    assert TurnLimits().over(DEFAULT_LIMITS).deadline_s is None


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        TurnLimits(deadline_s=0)


# ─────────────────────────── 常量 ───────────────────────────


def test_twelve_methods_split_by_direction() -> None:
    assert len(methods.SERVER_TO_BRIDGE) == 6 and len(methods.BRIDGE_TO_SERVER) == 6
    assert not methods.SERVER_TO_BRIDGE & methods.BRIDGE_TO_SERVER
    # 方法名用点号，与 ACP 的斜杠形式区分（§4.1 P5）
    assert all(
        "." in m and "/" not in m for m in methods.SERVER_TO_BRIDGE | methods.BRIDGE_TO_SERVER
    )


@pytest.mark.parametrize(
    ("code", "reconnect"),
    [
        (CloseCode.SUPERSEDED, False),
        (CloseCode.SESSION_GONE, False),
        (CloseCode.GOING_AWAY, True),
        (CloseCode.INTERNAL_ERROR, True),
        (CloseCode.ABNORMAL, True),
        (None, True),
    ],
)
def test_should_reconnect(code: int | None, reconnect: bool) -> None:
    assert should_reconnect(code) is reconnect
