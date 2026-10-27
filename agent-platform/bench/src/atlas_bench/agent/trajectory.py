"""归档轨迹 → 过程指标（doc/agent-eval-design.html §8.2 / §9）。

输入是 Atlas 的 GET /v1/runs/{id}/trace（TraceEvent 列表，含子 run）与 GET /v1/runs/{id}（用量）。
只算结构化指标；正文不进结果文件（原始轨迹另存 raw/，供人工复核）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

__all__ = ["check_expectations", "metrics_from_trace"]

_EXIT = re.compile(r"\[Command failed with exit code (\d+)\]")
#: 同一工具、同一参数在这么多次工具调用之内重复出现 → 记一次重复调用
_REPEAT_WINDOW = 5
#: 沙箱不可用被中间件转成了工具错误（不让 run 失败）—— 这是平台问题，要单独数出来，
#: 否则它会伪装成「Agent 没做成」（曾因沙箱 Pod 配额满，一题「无法验证」）
_SANDBOX_DOWN = "Error: Sandbox unavailable"


def _digest(args: Any) -> str:
    return hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]


def metrics_from_trace(events: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = tool_errors = shell_calls = shell_failures = 0
    approvals = compactions = subagents = todo_updates = files_written = repeats = 0
    cleared = sandbox_down = 0
    #: Agent 向用户提问（ask_user_question）的次数与题数 —— 问题走审批同一个事件，但不算审批
    question_rounds = questions_asked = 0
    by_tool: dict[str, int] = {}
    recent: list[str] = []
    #: 同时在跑的（主 run 直接发起的）子智能体：started +1，finished -1（挂起的不算结束）
    running: set[str] = set()
    max_parallel = 0
    for e in events:
        t, d = e.get("type"), e.get("data") or {}
        if t == "tool.started":
            tool_calls += 1
            name = str(d.get("name") or "")
            by_tool[name] = by_tool.get(name, 0) + 1
            key = f"{name}:{_digest(d.get('args'))}"
            if key in recent[-_REPEAT_WINDOW:]:
                repeats += 1
            recent.append(key)
            if name == "execute":
                shell_calls += 1
        elif t == "tool.failed":
            tool_errors += 1
            if str(d.get("result") or "").startswith(_SANDBOX_DOWN):
                sandbox_down += 1
        elif t == "tool.completed" and d.get("name") == "execute":
            if _EXIT.search(str(d.get("result") or "")):
                shell_failures += 1
        elif t == "approval.required" and d.get("approval_kind") == "question":
            question_rounds += 1
            questions_asked += len((d.get("args") or {}).get("questions") or [])
        elif t == "approval.required":
            approvals += 1
        elif t == "context.compacted":
            compactions += 1
        elif t == "context.cleared":
            cleared += 1
        elif t == "subagent.started":
            subagents += 1
            if int(e.get("depth") or 0) == 0:
                running.add(str(d.get("subagent_run_id") or d.get("call_id") or len(running)))
                max_parallel = max(max_parallel, len(running))
        elif t == "subagent.finished" and not d.get("suspended"):
            running.discard(str(d.get("subagent_run_id") or d.get("call_id") or ""))
        elif t == "todos.updated":
            todo_updates += 1
        elif t == "file.written":
            files_written += 1

    def total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in runs)

    return {
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "shell_calls": shell_calls,
        "shell_failures": shell_failures,
        "repeated_calls": repeats,
        "approvals": approvals,
        "question_rounds": question_rounds,
        "questions_asked": questions_asked,
        "compactions": compactions,
        "context_cleared": cleared,
        "subagent_calls": subagents,
        "max_parallel_subagents": max_parallel,
        "sandbox_unavailable": sandbox_down,
        "todo_updates": todo_updates,
        "files_written": files_written,
        "by_tool": by_tool,
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "total_tokens": total("total_tokens"),
        "runs": len(runs),
    }


_CMP = re.compile(r"^(>=|<=|==|>|<)(\d+)$")


def _compare(actual: int, expr: str) -> bool:
    m = _CMP.match(expr)
    if not m:
        return False
    op, n = m.group(1), int(m.group(2))
    return {
        ">=": actual >= n,
        "<=": actual <= n,
        "==": actual == n,
        ">": actual > n,
        "<": actual < n,
    }[op]


def check_expectations(
    expect: list[Any], events: list[dict[str, Any]], metrics: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool | None]:
    """过程断言 → ([{desc, ok, actual, count}], 全部通过？)。没有断言时返回 ([], None)。"""
    if not expect:
        return [], None
    out = []
    for x in expect:
        if x.event:
            actual = sum(
                1
                for e in events
                if e.get("type") == x.event
                and all((e.get("data") or {}).get(k) == v for k, v in x.where.items())
            )
            what = x.event + (f" {x.where}" if x.where else "")
        else:
            actual = int(metrics.get(x.metric or "") or 0)
            what = f"指标 {x.metric}"
        out.append(
            {
                "desc": x.desc or what,
                "ok": _compare(actual, x.count),
                "actual": actual,
                "count": x.count,
                "gate": x.gate,
            }
        )
    # expect_ok 只看不变量；场景前提单独统计（report 里的「场景未生效」）
    return out, all(r["ok"] for r in out if r["gate"])
