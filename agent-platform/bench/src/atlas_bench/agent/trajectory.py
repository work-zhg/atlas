"""归档轨迹 → 过程指标（doc/agent-eval-design.html §11.2 / §11.3）。

输入是 Atlas 的 GET /v1/runs/{id}/trace（TraceEvent 列表，含子 run）与 GET /v1/runs/{id}（用量）。
只算结构化指标；正文不进结果文件（原始轨迹另存 raw/，供人工复核）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

__all__ = ["metrics_from_trace"]

_EXIT = re.compile(r"\[Command failed with exit code (\d+)\]")
#: 同一工具、同一参数在这么多次工具调用之内重复出现 → 记一次重复调用
_REPEAT_WINDOW = 5


def _digest(args: Any) -> str:
    return hashlib.sha1(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]


def metrics_from_trace(events: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    tool_calls = tool_errors = shell_calls = shell_failures = 0
    approvals = compactions = subagents = todo_updates = files_written = repeats = 0
    by_tool: dict[str, int] = {}
    recent: list[str] = []
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
        elif t == "tool.completed" and d.get("name") == "execute":
            if _EXIT.search(str(d.get("result") or "")):
                shell_failures += 1
        elif t == "approval.required":
            approvals += 1
        elif t == "context.compacted":
            compactions += 1
        elif t == "subagent.started":
            subagents += 1
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
        "compactions": compactions,
        "subagent_calls": subagents,
        "todo_updates": todo_updates,
        "files_written": files_written,
        "by_tool": by_tool,
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "total_tokens": total("total_tokens"),
        "runs": len(runs),
    }
