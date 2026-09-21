"""★ §7.3 坑 1 的回归护栏：压缩边界不得拆散 tool_use / tool_result。

文档明说这个坑"在短对话里永远暴露不出来"，所以核心用例是
**构造 30 轮带工具调用的历史强制触发压缩**。
"""

from __future__ import annotations

import pytest
from atlas_engine.kernel.middleware._compaction import (
    MessageFacts,
    drop_thinking,
    plan_split,
    safe_boundaries,
    would_split_tool_pair,
)


def build_history(turns: int, *, tools_per_turn: int = 2) -> list[MessageFacts]:
    """每轮 = user 提问 → assistant 发 N 个 tool_use → user 回 N 个 tool_result
    → assistant 给正文。

    这正是 kernel 跑起来后的真实形状：坑 1 只在这种形状下才会显形。
    """
    facts: list[MessageFacts] = []
    idx = 0

    def push(**kw) -> None:
        nonlocal idx
        facts.append(MessageFacts(index=idx, **kw))
        idx += 1

    for t in range(turns):
        ids = frozenset(f"toolu_{t}_{i}" for i in range(tools_per_turn))
        push(starts_turn=True)  # user 提问（真实输入，开启新回合）
        push(issues=ids)  # assistant: tool_use ×N
        push(answers=ids)  # user: tool_result ×N（延续，非新回合）
        push()  # assistant: 正文
    return facts


# ---------------------------------------------------------------------------
# 坑 1
# ---------------------------------------------------------------------------


def test_naive_split_would_break_tool_pairs() -> None:
    """先证明"朴素按条数切"确实危险 —— 否则本模块没有存在的理由。"""
    facts = build_history(30)
    # 切在 assistant 发出 tool_use 之后、user 回 tool_result 之前
    dangerous = [i for i in range(1, len(facts)) if would_split_tool_pair(facts, i)]
    assert dangerous, "构造的历史里应当存在会拆散配对的切点"
    # 每轮 4 条，第 2 条之后就是危险切点
    assert would_split_tool_pair(facts, 2) is True


def test_planned_split_never_breaks_tool_pairs_30_turns() -> None:
    """★ 30 轮带工具调用，强制触发压缩，切点必须安全。"""
    facts = build_history(30)
    idx = plan_split(facts, keep_recent_turns=3)

    assert idx > 0, "30 轮应当能压缩"
    assert would_split_tool_pair(facts, idx) is False, (
        f"切点 {idx} 拆散了 tool_use/tool_result —— 这会让 API 直接 400"
    )


@pytest.mark.parametrize("turns", [4, 10, 30, 100])
@pytest.mark.parametrize("tools_per_turn", [1, 3])
@pytest.mark.parametrize("keep", [1, 3, 5])
def test_split_is_always_safe(turns: int, tools_per_turn: int, keep: int) -> None:
    """把参数组合扫一遍：任何情况下切点都必须是安全边界。"""
    facts = build_history(turns, tools_per_turn=tools_per_turn)
    idx = plan_split(facts, keep_recent_turns=keep)
    assert would_split_tool_pair(facts, idx) is False


def test_split_keeps_requested_number_of_turns() -> None:
    facts = build_history(10)
    idx = plan_split(facts, keep_recent_turns=3)
    kept_turns = [f for f in facts[idx:] if f.starts_turn]
    assert len(kept_turns) == 3


def test_no_compaction_when_history_is_short() -> None:
    facts = build_history(2)
    assert plan_split(facts, keep_recent_turns=3) == 0


def test_unclosed_tool_call_at_tail_is_not_a_boundary() -> None:
    """本轮还没跑完（tool_use 未被回应）时，末尾不是安全切点。"""
    facts = build_history(5)
    facts.append(MessageFacts(index=len(facts), issues=frozenset({"toolu_open"})))
    assert safe_boundaries(facts)[-1] != len(facts)
    idx = plan_split(facts, keep_recent_turns=2)
    assert would_split_tool_pair(facts, idx) is False


def test_history_with_no_tools_still_splits() -> None:
    """纯文本对话没有配对问题，但仍要能正常压缩。"""
    facts = [MessageFacts(index=i, starts_turn=(i % 2 == 0)) for i in range(20)]
    idx = plan_split(facts, keep_recent_turns=3)
    assert idx == 14  # 保留最后 3 个 user 回合（下标 14/16/18）
    assert would_split_tool_pair(facts, idx) is False


def test_keep_recent_turns_must_be_positive() -> None:
    with pytest.raises(ValueError):
        plan_split(build_history(5), keep_recent_turns=0)


# ---------------------------------------------------------------------------
# 坑 3：压缩区内的 thinking block 丢弃，不送去摘要
# ---------------------------------------------------------------------------


def test_thinking_blocks_in_compressed_region_are_dropped() -> None:
    facts = [
        MessageFacts(index=0),
        MessageFacts(index=1, is_thinking_only=True),
        MessageFacts(index=2),
        MessageFacts(index=3, is_thinking_only=True),
    ]
    assert drop_thinking(facts, upto=3) == [1]
    assert drop_thinking(facts, upto=4) == [1, 3]
