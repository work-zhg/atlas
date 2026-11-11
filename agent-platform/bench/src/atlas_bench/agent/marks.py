"""计数脚本的指标（doc/sandbox-eng-cases.html §0「副作用计数」）。

被测命令调用套件共享的 marks/mark.sh <标记> <秒数>：
  · 开始时建 marks/<标记>/start-<id>，内容带沙箱的 hostname（就是 Pod 名）
  · 每秒打印一行 tick <n>；结束时建 end-<id>（带随机令牌），最后一行打印「MARK-DONE <标记> <令牌>」
  · 每次执行建新文件而不是追加：/workspace 是 s3fs 挂载，并发追加会互相覆盖

    marks.<标记>.start        执行了几次（start 文件数）
    marks.<标记>.end          跑完了几次（end 文件数）
    marks.<标记>.interrupted  start − end：有几次执行没跑完（被打断）。模型打断后自己重跑也看得出来
    marks.<标记>.calls        轨迹里命令调用了它的工具调用有几个（按 tool_call_id 去重）
    marks.<标记>.token_seen   这些调用的工具结果里出现了几个 end 令牌 —— 模型拿到的是真实输出
    marks.<标记>.seq_broken   有几个工具结果里的 tick 序号断档或重复（断线续读丢了 / 重了）
    marks.<标记>.approvals    调用它的命令被请示了几次（接管重放时不该再问一次）
    platform_reruns           Σ max(0, start − calls)：平台替模型多跑的次数（重放时重新提交）。
                              只算有调用直接点名它的标记（经脚本间接调用的无从对照）
    unapproved_runs           Σ max(0, start − 批准次数)：执行次数超过批准次数（被拒的命令也跑了）。
                              只算有审批记录的标记（自动模式直接放行的没有记录，无从比较）
    sandbox_hosts             所有 start 文件里不同的 hostname 个数（会话用过几个沙箱）

★ 模型自己决定重做（新的 tool_call_id）不算平台重跑：calls 会跟着加一。
★ 认调用看参数里的 command，不看工具名：CLI 的 Shell 工具名是命令本身的第一行。CLI 一开始只报
  工具名、命令随后补全（tool.updated），所以同一个调用的开始、补全、终态里的参数都要看。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MARK_DIR", "MarkReport", "command_of", "mark_report"]

MARK_DIR = "marks"
#: 命令里调用计数脚本的位置（一条命令可以调好几次）
#: 标记以字母开头：cat marks/mark.sh 2>&1 里的 2 不是标记
_CALL = re.compile(r"mark\.sh\s+([A-Za-z][\w-]*)")
_FIELD = re.compile(r"(\w+)=(\S+)")
#: 计数脚本每秒打印的一行
_TICK = re.compile(r"^tick (\d+)$")
#: 带工具参数的事件（开始、参数补全、终态）
_TOOL_EVENTS = frozenset({"tool.started", "tool.updated", "tool.completed", "tool.failed"})


@dataclass
class MarkReport:
    metrics: dict[str, int] = field(default_factory=dict)
    #: 会话用过的沙箱 Pod 名（start 文件里的 hostname）
    hosts: set[str] = field(default_factory=set)


def command_of(args: Any) -> str:
    """工具参数里的命令：原生 execute 是 {command}；CLI 审批里的 toolCall 把参数放在 rawInput 下。

    ★ 不看工具名：CLI 的 Shell 工具名是命令本身的第一行。
    """
    if not isinstance(args, dict):
        return ""
    raw = args.get("rawInput")
    if isinstance(raw, dict) and raw.get("command"):
        return str(raw["command"])
    return str(args.get("command") or "")


def _fields(data: bytes | str) -> dict[str, str]:
    text = data.decode(errors="replace") if isinstance(data, bytes) else data
    return dict(_FIELD.findall(text))


def _seq_broken(text: str) -> bool:
    """输出里计数脚本的 tick 行应当是 1、2、3…… 连续不重复。没有 tick 行不算断档。"""
    nums = [int(m.group(1)) for line in text.splitlines() if (m := _TICK.match(line.strip()))]
    return bool(nums) and nums != list(range(1, len(nums) + 1))


def mark_report(
    files: dict[str, bytes],
    trace: list[dict[str, Any]],
    approvals: list[dict[str, Any]] | None = None,
) -> MarkReport:
    """approvals：评测批过的审批（platforms/atlas.py 的 TrialRun.approvals，带 command）。"""
    starts: dict[str, int] = defaultdict(int)
    ends: dict[str, int] = defaultdict(int)
    tokens: dict[str, set[str]] = defaultdict(set)
    report = MarkReport()
    for path, data in files.items():
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != MARK_DIR:
            continue
        label, name = parts[1], parts[2]
        info = _fields(data)
        if name.startswith("start-"):
            starts[label] += 1
            if host := info.get("host"):
                report.hosts.add(host)
        elif name.startswith("end-"):
            ends[label] += 1
            if token := info.get("token"):
                tokens[label].add(token)

    calls: dict[str, set[str]] = defaultdict(set)
    results: dict[str, str] = {}
    for e in trace:
        t, d = e.get("type"), e.get("data") or {}
        call_id = str(d.get("call_id") or "")
        if t not in _TOOL_EVENTS or not call_id:
            continue
        for label in _CALL.findall(command_of(d.get("args"))):
            calls[label].add(call_id)
        if t in ("tool.completed", "tool.failed"):
            results[call_id] = str(d.get("result") or "")

    asked: dict[str, int] = defaultdict(int)
    approved: dict[str, int] = defaultdict(int)
    for a in approvals or []:
        for label in set(_CALL.findall(str(a.get("command") or ""))):
            asked[label] += 1
            approved[label] += a.get("decision") == "approved"

    reruns = unapproved = 0
    for label in sorted(set(starts) | set(ends) | set(calls)):
        texts = [results[c] for c in calls[label] if c in results]
        m = report.metrics
        m[f"marks.{label}.start"] = starts[label]
        m[f"marks.{label}.end"] = ends[label]
        m[f"marks.{label}.interrupted"] = max(0, starts[label] - ends[label])
        m[f"marks.{label}.calls"] = len(calls[label])
        m[f"marks.{label}.token_seen"] = sum(
            1 for tok in tokens[label] if any(tok in text for text in texts)
        )
        m[f"marks.{label}.seq_broken"] = sum(1 for text in texts if _seq_broken(text))
        m[f"marks.{label}.approvals"] = asked[label]
        if calls[label]:
            reruns += max(0, starts[label] - len(calls[label]))
        if asked[label]:
            unapproved += max(0, starts[label] - approved[label])
    report.metrics["platform_reruns"] = reruns
    report.metrics["unapproved_runs"] = unapproved
    report.metrics["sandbox_hosts"] = len(report.hosts)
    return report
