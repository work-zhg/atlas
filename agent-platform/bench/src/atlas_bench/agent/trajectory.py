"""归档轨迹 → 过程指标（doc/agent-eval-design.html §8.2 / §9）。

输入是 Atlas 的 GET /v1/runs/{id}/trace（TraceEvent 列表，含子 run）与 GET /v1/runs/{id}（用量）。
只算结构化指标；正文不进结果文件（原始轨迹另存 raw/，供人工复核）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

__all__ = ["check_expectations", "event_invariants", "metrics_from_trace"]

_EXIT = re.compile(r"\[Command failed with exit code (\d+)\]")
#: 同一工具、同一参数在这么多次工具调用之内重复出现 → 记一次重复调用
_REPEAT_WINDOW = 5
#: 沙箱错误被中间件转成了工具错误（不让 run 失败），平台在结果的 artifact.sandbox_error 里给类别：
#:   unavailable     沙箱起不来 / 拒绝命令 —— 平台问题，要单独数出来，否则它会伪装成「Agent 没做成」
#:                   （曾因沙箱 Pod 配额满，一题「无法验证」）
#:   lost            命令执行中沙箱没了（被删、驱逐、execd 重启）
#:   result_unknown  提交时断开（或上一个执行者死在提交途中）：命令可能执行了，结果未知
#: 后两类在注入故障的用例里是预期内的，由 runner 按任务决定算不算平台错误。
_SANDBOX_KINDS = {"unavailable": "sandbox_unavailable", "lost": "sandbox_lost",
                  "result_unknown": "sandbox_result_unknown"}  # fmt: skip
#: 平台还没带 sandbox_error 属性时（旧版本）的退路：按原来的错误前缀记成 unavailable
_SANDBOX_DOWN_LEGACY = "Error: Sandbox unavailable"


def _digest(args: Any) -> str:
    return hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]


def metrics_from_trace(events: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = tool_errors = shell_calls = shell_failures = 0
    approvals = compactions = subagents = todo_updates = files_written = repeats = 0
    cleared = rebuilt = 0
    sandbox: dict[str, int] = dict.fromkeys(_SANDBOX_KINDS.values(), 0)
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
            artifact = d.get("artifact") if isinstance(d.get("artifact"), dict) else {}
            kind = artifact.get("sandbox_error")
            if kind in _SANDBOX_KINDS:
                sandbox[_SANDBOX_KINDS[kind]] += 1
            elif kind is None and str(d.get("result") or "").startswith(_SANDBOX_DOWN_LEGACY):
                sandbox["sandbox_unavailable"] += 1
        elif t == "tool.completed" and d.get("name") == "execute":
            if _EXIT.search(str(d.get("result") or "")):
                shell_failures += 1
            # 换了沙箱之后的第一条命令：平台在输出前加了「环境已重建」提示
            if (d.get("artifact") or {}).get("sandbox_rebuilt"):
                rebuilt += 1
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
        **sandbox,
        "sandbox_rebuilt": rebuilt,
        "todo_updates": todo_updates,
        "files_written": files_written,
        "by_tool": by_tool,
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "total_tokens": total("total_tokens"),
        "runs": len(runs),
    }


#: 一轮的终止事件
_TERMINAL = frozenset({"run.finished", "run.failed", "run.cancelled"})
_TOOL_TERMINAL = frozenset({"tool.completed", "tool.failed"})
#: 工具名的显示上限：一行、80 字，超了截断再加一个省略号（cli/translate/acp_v1.py 的 _short_title）
_NAME_MAX = 81
_RUN_DONE = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def _name_bad(name: Any) -> bool:
    text = str(name or "")
    return not text.strip() or "\n" in text or len(text) > _NAME_MAX


def event_invariants(
    trace: list[dict[str, Any]], stream: list[dict[str, Any]], runs: list[dict[str, Any]]
) -> dict[str, int]:
    """事件级不变量（doc/cli-func-cases.html §0、EVT-01～03）→ 违反的次数，0 = 守住了。

    trace 是归档轨迹（不含增量），stream 是评测在会话流上收到的全部事件（含增量）。正文一致、
    序号连续只能对着 stream 查，且只查流上见到了终止事件的 run（没见到 = 流没收全，记
    stream_incomplete，不冤枉平台）。

        answer_mismatch          message.completed 的正文 ≠ 这个 run 的 message.delta 依次拼接
        seq_gaps · seq_dups      run 内序号（seq）从 1 起有缺口 / 有重复
        tool_started_dup         同一个调用不止一条 tool.started
        tool_terminal_dup        同一个调用不止一条终态（completed / failed）
        tool_terminal_missing    正常结束的 run 里，开始了却没有终态的调用
        tool_name_bad            工具名为空、多行或超过 81 字
        tool_failed_no_error     tool.failed 没带 error（前端的工具行显示「无错误详情」）
        call_ids_reused          工具调用的 call_id 出现在不止一个 run 里（旧调用被下一轮重放）
        subagents_unfinished     subagent.started 多于（不是挂起的）subagent.finished
        usage_no_model           CLI run 的 usage.updated 没带模型名（遥测没法计价）
        usage_cache_over_input   usage.updated 的 input_tokens < cache_read
                                 （口径不是「input 含缓存」）
        cli_runs_no_context_usage  正常结束的 CLI run 一条 context.usage（basis = cli）都没有
        runs_nonterminal · runs_zero_usage
                                 没有终态的 run；成功了用量却是 0 的 run
        approval_dup             approval.required 条数 − 不同的审批 id 数（同一审批重复弹出）
        approval_events_sub      子 run（depth ≥ 1）上的 approval.required
        todos_open_last          最后一次计划快照里还没完成的项
    """
    # 断线重连可能重复收到同一条：按会话序号去重（不同序号的同样内容是平台重发，要数出来）
    seen: set[int] = set()
    events: list[dict[str, Any]] = []
    for e in stream:
        ts = int(e.get("thread_seq") or 0)
        if ts and ts in seen:
            continue
        seen.add(ts)
        events.append(e)
    ended = {str(e.get("run_id")) for e in events if e.get("type") in _TERMINAL}
    finished = {str(e.get("run_id")) for e in trace if e.get("type") == "run.finished"}
    cli_runs = {
        str(e.get("run_id"))
        for e in trace
        if e.get("type") == "run.started" and "permission_mode" in (e.get("data") or {})
    }

    deltas: dict[str, list[str]] = {}
    completed: dict[str, list[str]] = {}
    seqs: dict[str, list[int]] = {}
    for e in events:
        rid, t, d = str(e.get("run_id")), e.get("type"), e.get("data") or {}
        if isinstance(e.get("seq"), int):
            seqs.setdefault(rid, []).append(e["seq"])
        if t == "message.delta":
            deltas.setdefault(rid, []).append(str(d.get("text") or ""))
        elif t == "message.completed":
            completed.setdefault(rid, []).append(_text(d.get("content")))
    mismatch = sum(
        1
        for rid, parts in completed.items()
        if rid in ended and "".join(parts) != "".join(deltas.get(rid, []))
    )
    gaps = dups = 0
    for rid, values in seqs.items():
        if rid not in ended:
            continue
        dups += len(values) - len(set(values))
        gaps += len(set(range(1, max(values) + 1)) - set(values))

    started: dict[tuple[str, str], int] = {}
    terminal: dict[tuple[str, str], int] = {}
    call_runs: dict[str, set[str]] = {}
    names_bad = no_model = cache_bad = approval_events = approval_sub = no_error = 0
    subagents = 0
    approval_ids: set[str] = set()
    context_runs: set[str] = set()
    todos: list[dict[str, Any]] | None = None
    for e in trace:
        rid, t, d = str(e.get("run_id")), e.get("type"), e.get("data") or {}
        key = (rid, str(d.get("call_id") or ""))
        if t in ("tool.started", "tool.updated", *_TOOL_TERMINAL) and key[1]:
            call_runs.setdefault(key[1], set()).add(rid)
        if t == "tool.started":
            started[key] = started.get(key, 0) + 1
            names_bad += _name_bad(d.get("name"))
        elif t == "tool.updated" and "name" in d:
            names_bad += _name_bad(d.get("name"))
        elif t in _TOOL_TERMINAL:
            terminal[key] = terminal.get(key, 0) + 1
            no_error += t == "tool.failed" and not d.get("error")
        elif t == "subagent.started":
            subagents += 1
        elif t == "subagent.finished" and not d.get("suspended"):
            subagents -= 1
        elif t == "usage.updated":
            no_model += rid in cli_runs and not d.get("model")
            cache_bad += int(d.get("input_tokens") or 0) < int(d.get("cache_read") or 0)
        elif t == "context.usage" and d.get("basis") == "cli":
            context_runs.add(rid)
        elif t == "approval.required" and d.get("approval_kind") != "question":
            approval_events += 1
            approval_sub += int(e.get("depth") or 0) > 0
            approval_ids.add(str(d.get("approval_id") or ""))
        elif t == "todos.updated":
            todos = d.get("todos") if isinstance(d.get("todos"), list) else []

    run_ids = {str(r.get("id")) for r in runs}
    return {
        "answer_mismatch": mismatch,
        "seq_gaps": gaps,
        "seq_dups": dups,
        "stream_incomplete": len(run_ids - ended),
        "tool_started_dup": sum(1 for n in started.values() if n > 1),
        "tool_terminal_dup": sum(1 for n in terminal.values() if n > 1),
        "tool_terminal_missing": sum(
            1 for key in started if key[0] in finished and key not in terminal
        ),
        "tool_name_bad": names_bad,
        "tool_failed_no_error": no_error,
        "call_ids_reused": sum(1 for runs_of in call_runs.values() if len(runs_of) > 1),
        "subagents_unfinished": max(0, subagents),
        "cli_runs": len(cli_runs),
        "usage_no_model": no_model,
        "usage_cache_over_input": cache_bad,
        "cli_runs_no_context_usage": len((cli_runs & finished) - context_runs),
        "runs_nonterminal": sum(1 for r in runs if r.get("status") not in _RUN_DONE),
        "runs_zero_usage": sum(
            1 for r in runs if r.get("status") == "succeeded" and not r.get("total_tokens")
        ),
        "approval_events": approval_events,
        "approval_dup": approval_events - len(approval_ids),
        "approval_events_sub": approval_sub,
        "todos_open_last": sum(
            1 for x in todos or [] if isinstance(x, dict) and x.get("status") != "completed"
        ),
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
