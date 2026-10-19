"""SubAgent 结论整形（format_subagent_result）：成功 / 空结论 / 失败带部分结果 / 统计行。"""

from __future__ import annotations

from atlas_agent.services.subagent import format_subagent_result

BASE = {"error": None, "tool_calls": 3, "tokens": 1200, "duration_s": 12.4, "persistent": True}


def test_success_returns_conclusion_with_usage_trailer() -> None:
    out = format_subagent_result("coder", status="succeeded", text="改好了 a.py", **BASE)
    body, trailer = out.rsplit("\n\n", 1)
    assert body == "改好了 a.py"
    assert trailer == (
        '<subagent subagent="coder" status="succeeded" session="persistent" '
        'tokens="1200" tool_calls="3" duration_s="12"/>'
    )


def test_empty_success_is_not_read_as_no_findings() -> None:
    out = format_subagent_result("coder", status="succeeded", text="", **BASE)
    assert "没有给出文字结论" in out and "不等于「没有发现」" in out


def test_failure_carries_partial_output_and_reason() -> None:
    kw = {**BASE, "error": "模型调用失败"}
    out = format_subagent_result("scout", status="failed", text="已查到 3 个候选", **kw)
    assert out.startswith("scout 以 failed 结束：模型调用失败。")
    assert "部分结果，可能不完整" in out and "已查到 3 个候选" in out
    assert 'status="failed"' in out


def test_cancelled_without_output_says_so_and_partial_is_clipped() -> None:
    out = format_subagent_result("scout", status="cancelled", text="", **BASE)
    assert "没有留下文字输出" in out
    long = format_subagent_result("scout", status="interrupted", text="字" * 5000, **BASE)
    assert "（已截断）" in long and long.count("字") == 4000


def test_ephemeral_and_unknown_duration() -> None:
    kw = {**BASE, "persistent": False, "duration_s": None}
    out = format_subagent_result("scout", status="succeeded", text="ok", **kw)
    assert 'session="ephemeral"' in out and "duration_s" not in out
