"""汇总与报告（doc/agent-eval-design.html §15）。

    成功率     每题 k 次的均值 → 题目层面平均；bootstrap（按题重采样）95% 置信区间
    pass^k     k 次全成功的题目占比；与成功率差距大 = 不稳定
    配对比较   同一题上两个配置的成功率差（按题配对 bootstrap），区间不含 0 才算显著
    平台错误   infra_error 不进成功率分母，单列在首页

产出 summary.json + report.md + report.html（与 RAG 评测同一套表格渲染）。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from ..report import _html, _md
from ..stats import estimate, paired_diff
from .schema import TrialResult

__all__ = ["summarize", "write_agent_report"]

Table = tuple[list[str], list[list[Any]]]


def _load(run_dir: Path) -> dict[str, list[TrialResult]]:
    out: dict[str, list[TrialResult]] = {}
    for p in sorted((run_dir / "trials").glob("*.jsonl")):
        rows = [TrialResult.model_validate_json(x) for x in p.read_text("utf-8").splitlines() if x]
        # 续跑时同一 (task, trial) 可能有多行（infra_error 后重跑）：取最后一行
        last: dict[tuple[str, int], TrialResult] = {}
        for r in rows:
            last[(r.task_id, r.trial)] = r
        out[p.stem] = list(last.values())
    return out


def _per_task(rows: list[TrialResult]) -> dict[str, list[TrialResult]]:
    d: dict[str, list[TrialResult]] = defaultdict(list)
    for r in rows:
        if r.outcome != "infra_error":
            d[r.task_id].append(r)
    return d


def _avg(rows: list[TrialResult], key: str) -> float | None:
    vals = [float(r.metrics.get(key) or 0) for r in rows]
    return mean(vals) if vals else None


def summarize(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    data = _load(run_dir)
    order = [v for v in manifest["config"]["variants"] if v in data]
    variants: dict[str, Any] = {}
    task_success: dict[str, dict[str, float]] = {}
    for v in order:
        rows = data[v]
        tasks = _per_task(rows)
        ok_rows = [r for r in rows if r.outcome != "infra_error"]
        per_task = {t: mean(1.0 if r.passed else 0.0 for r in rs) for t, rs in tasks.items()}
        task_success[v] = per_task
        tool_calls = sum(int(r.metrics.get("tool_calls") or 0) for r in ok_rows)
        tool_errors = sum(int(r.metrics.get("tool_errors") or 0) for r in ok_rows)
        outcomes: dict[str, int] = defaultdict(int)
        for r in rows:
            outcomes[r.outcome] += 1
        variants[v] = {
            "success": estimate(list(per_task.values())).__dict__,
            "score": estimate([mean(r.score for r in rs) for rs in tasks.values()]).__dict__,
            "pass_all_k": (
                mean(1.0 if all(r.passed for r in rs) else 0.0 for rs in tasks.values())
                if tasks
                else None
            ),
            "trials": len(rows),
            "outcomes": dict(outcomes),
            # ★ 结果对了、但 run 没有正常结束（失败 / 中断）：成功率看不出来，平台问题会被掩盖
            "abnormal_end": sum(
                1 for r in ok_rows if r.passed and r.error and r.outcome == "success"
            ),
            "wall_s": mean(r.wall_s for r in ok_rows) if ok_rows else None,
            "total_tokens": _avg(ok_rows, "total_tokens"),
            "tool_calls": _avg(ok_rows, "tool_calls"),
            "tool_error_rate": tool_errors / tool_calls if tool_calls else None,
            "shell_failures": _avg(ok_rows, "shell_failures"),
            "repeated_calls": _avg(ok_rows, "repeated_calls"),
            "approvals": _avg(ok_rows, "approvals"),
            "compactions": _avg(ok_rows, "compactions"),
            "subagent_calls": _avg(ok_rows, "subagent_calls"),
        }
    comparisons = []
    for base, other in manifest["config"].get("comparisons") or []:
        if base in task_success and other in task_success:
            est, sig = paired_diff(task_success[base], task_success[other])
            comparisons.append(
                {"base": base, "other": other, "diff": est.__dict__, "significant": sig}
            )
    summary = {
        "name": manifest["config"]["name"],
        "variants": variants,
        "order": order,
        "comparisons": comparisons,
        "task_success": task_success,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _pct(e: dict[str, Any] | float | None) -> str:
    if e is None:
        return "—"
    if isinstance(e, dict):
        if not e.get("n"):
            return "—"
        return f"{e['mean'] * 100:.1f}%（{e['low'] * 100:.1f}–{e['high'] * 100:.1f}）"
    return f"{e * 100:.1f}%"


def _n(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def _sections(run_dir: Path, summary: dict[str, Any]) -> list[tuple[str, str | Table]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = manifest["config"]
    data = _load(run_dir)
    order = summary["order"]
    tasks = manifest["tasks"]
    secs: list[tuple[str, str | Table]] = []

    secs.append(
        (
            "实验",
            f"{cfg['name']}：{len(tasks)} 个任务 × {len(order)} 个配置 × 每题 {cfg['trials']} 次；"
            f"审批策略 {cfg['approval_policy']}；commit {manifest['git_commit']}；"
            f"开始于 {manifest['started_at']}。",
        )
    )
    secs.append(
        (
            "被测配置",
            (
                ["配置", "说明", "智能体", "类型", "模型", "工具"],
                [
                    [
                        v,
                        cfg["variants"][v].get("description", ""),
                        manifest["agents"][v]["slug"],
                        cfg["variants"][v]["agent"].get("kind", "native"),
                        (cfg["variants"][v]["agent"].get("model") or {}).get("model", ""),
                        "、".join(cfg["variants"][v]["agent"].get("tool_names") or []),
                    ]
                    for v in order
                ],
            ),
        )
    )
    vs = summary["variants"]
    secs.append(
        (
            "主要结果",
            (
                [
                    "配置",
                    "成功率（95% CI）",
                    "pass^k",
                    "部分得分",
                    "平台错误",
                    "超预算",
                    "异常结束",
                    "平均耗时 s",
                    "平均 token",
                    "工具调用",
                    "工具错误率",
                    "命令失败",
                    "重复调用",
                    "审批",
                ],
                [
                    [
                        v,
                        _pct(vs[v]["success"]),
                        _pct(vs[v]["pass_all_k"]),
                        _pct(vs[v]["score"]),
                        vs[v]["outcomes"].get("infra_error", 0),
                        vs[v]["outcomes"].get("budget_exceeded", 0),
                        vs[v].get("abnormal_end", 0),
                        _n(vs[v]["wall_s"], 0),
                        _n(vs[v]["total_tokens"], 0),
                        _n(vs[v]["tool_calls"]),
                        _pct(vs[v]["tool_error_rate"]),
                        _n(vs[v]["shell_failures"]),
                        _n(vs[v]["repeated_calls"]),
                        _n(vs[v]["approvals"]),
                    ]
                    for v in order
                ],
            ),
        )
    )
    if summary["comparisons"]:
        secs.append(
            (
                "配对比较（同一批任务上成功率之差：对照 − 基线）",
                (
                    ["基线", "对照", "差值（95% CI）", "结论"],
                    [
                        [
                            c["base"],
                            c["other"],
                            f"{c['diff']['mean'] * 100:+.1f} 个百分点（"
                            f"{c['diff']['low'] * 100:+.1f} ~ {c['diff']['high'] * 100:+.1f}）",
                            "显著" if c["significant"] else "不显著（样本不足以下结论）",
                        ]
                        for c in summary["comparisons"]
                    ],
                ),
            )
        )
    cats = sorted({t["category"] for t in tasks})
    if len(cats) > 1:
        by_cat = {t["id"]: t["category"] for t in tasks}
        rows = []
        for c in cats:
            row: list[Any] = [c]
            for v in order:
                vals = [s for t, s in summary["task_success"][v].items() if by_cat.get(t) == c]
                row.append(_pct(mean(vals)) if vals else "—")
            rows.append(row)
        secs.append(("按类别", (["类别", *order], rows)))

    per_task_rows = []
    for t in tasks:
        row: list[Any] = [t["id"], t["category"]]
        for v in order:
            rs = [r for r in data.get(v, []) if r.task_id == t["id"]]
            ok = sum(r.passed for r in rs if r.outcome != "infra_error")
            n = sum(1 for r in rs if r.outcome != "infra_error")
            infra = sum(1 for r in rs if r.outcome == "infra_error")
            row.append(f"{ok}/{n}" + (f"（+{infra} 平台错误）" if infra else ""))
        per_task_rows.append(row)
    secs.append(("逐题", (["任务", "类别", *order], per_task_rows)))

    fails = []
    for v in order:
        for r in sorted(data.get(v, []), key=lambda x: (x.task_id, x.trial)):
            if r.passed and not r.error:
                continue
            tail = (r.verifier_output or "").strip().splitlines()[-3:]
            fails.append(
                [
                    v,
                    f"{r.task_id}#{r.trial}",
                    r.outcome,
                    (r.error or "")[:200],
                    " / ".join(tail)[:300],
                    r.refs.get("thread_id", ""),
                ]
            )
    if fails:
        secs.append(
            ("失败明细", (["配置", "任务", "结果", "错误", "校验输出（末尾）", "会话"], fails))
        )
    secs.append(
        (
            "说明",
            "成功率按题平均（每题先求 k 次的均值）；"
            "平台错误（Pod 起不来、网关 / 接口异常、校验容器失败）不计入分母。"
            "超预算的 trial 记为失败。「异常结束」= 校验通过但 run 没有正常结束（失败 / 中断），"
            "这类 trial 也列在失败明细里。"
            "校验在无网络的隔离容器里对最终工作区执行任务自带脚本，"
            "Agent 看不到校验脚本。原始轨迹在 raw/，可离线重判"
            "（python -m atlas_bench agent report <目录>）。",
        )
    )
    return secs


def write_agent_report(run_dir: Path) -> tuple[Path, Path]:
    summary = summarize(run_dir)
    sections = _sections(run_dir, summary)
    md, page = run_dir / "report.md", run_dir / "report.html"
    md.write_text(_md(sections).replace("# 评测报告", "# Agent 评测报告", 1), encoding="utf-8")
    page.write_text(_html(sections).replace("评测报告", "Agent 评测报告"), encoding="utf-8")
    return md, page
