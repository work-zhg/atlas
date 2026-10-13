"""评测报告（doc/rag-eval-design.html §13）：report.md + report.html。

配置 → 总表（含 95% 置信区间）→ 与基线的配对比较 → 分题型 → 错题归因 → 错题清单
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .datasets import find_dataset
from .settings import BenchSettings
from .stats import Estimate, paired_diff

__all__ = ["write_report"]

Table = tuple[list[str], list[list[str]]]


def _e(d: dict[str, Any] | None, pct: bool = True) -> str:
    if not d or not d.get("n"):
        return "—"
    return Estimate(d["mean"], d["low"], d["high"], d["n"]).fmt(pct)


def _num(v: float | None, digits: int = 0) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def _per_q(run_dir: Path, key: str) -> dict[str, dict[str, Any]]:
    ds, platform, variant = key.split("/")
    path = run_dir / "scores" / ds / f"{platform}__{variant}.jsonl"
    return {
        row["qid"]: row
        for row in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def _crag(row: dict[str, Any]) -> float | None:
    vals = [g["crag"] for g in row.get("generations") or [] if g.get("crag") is not None]
    return sum(vals) / len(vals) if vals else None


def _sections(
    run_dir: Path, summary: dict[str, Any], settings: BenchSettings
) -> list[tuple[str, str | Table]]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = summary["config"]
    out: list[tuple[str, str | Table]] = []

    out.append(
        (
            "配置",
            (
                ["项", "值"],
                [
                    ["实验", cfg["name"]],
                    [
                        "数据集",
                        "、".join(
                            f"{k}（{len(v['questions'])} 题，{v['documents']} 篇文档）"
                            for k, v in manifest["datasets"].items()
                        ),
                    ],
                    ["划分 / top_k / 重复", f"{cfg['split']} / {cfg['top_k']} / {cfg['repeats']}"],
                    ["生成", f"{cfg['generation']} · {cfg['generator_model']}"],
                    ["裁判", f"{cfg['judge_model']}（与生成模型不同）"],
                    [
                        "平台",
                        "、".join(
                            f"{k}：{v.get('base_url', '')}"
                            for k, v in manifest.get("platforms", {}).items()
                        ),
                    ],
                    ["代码版本", manifest.get("git_commit", "")],
                    [
                        "时间",
                        f"{manifest.get('started_at', '')} → {manifest.get('finished_at', '')}",
                    ],
                ],
            ),
        )
    )

    keys = list(summary["results"])
    rows = []
    for key in keys:
        r = summary["results"][key]
        g = r.get("generation") or {}
        rows.append(
            [
                key,
                _e(r["retrieval"].get("hit")),
                _e(r["retrieval"].get("recall")),
                _e(r["retrieval"].get("mrr"), pct=False),
                _e(r["retrieval"].get("ndcg"), pct=False),
                _num((r.get("context_chars") or {}).get("mean")),
                _e(g.get("crag"), pct=False),
                _e(g.get("accuracy")),
                _e(g.get("refusal_accuracy")),
                _e(g.get("false_refusal")),
                _e(g.get("faithfulness"), pct=False),
                _e(g.get("factual_correctness"), pct=False),
            ]
        )
    out.append(
        (
            "总表（括号内为 95% 置信区间）",
            (
                [
                    "数据集/平台/配置",
                    "命中率@k",
                    "召回率@k",
                    "MRR",
                    "nDCG",
                    "返回字数",
                    "CRAG 分",
                    "正确率",
                    "拒答正确率",
                    "误拒率",
                    "忠实度",
                    "事实正确性",
                ],
                rows,
            ),
        )
    )
    out.append(
        (
            "指标说明",
            "检索指标只统计有标准出处的题；"
            "CRAG 分：正确 +1、没答 0、答错 −1（拒答题拒答 +1、编造 −1），范围 −1～1；"
            "正确率 / 误拒率统计有答案的题，拒答正确率统计资料中没有答案的题；"
            "忠实度、事实正确性来自 RAGAS（0～1），不含拒答的回答。",
        )
    )

    # 同一数据集内，各配置与第一个配置（基线）配对比较
    by_ds: dict[str, list[str]] = {}
    for key in keys:
        by_ds.setdefault(key.split("/")[0], []).append(key)
    comp_rows = []
    for ks in by_ds.values():
        if len(ks) < 2:
            continue
        base = _per_q(run_dir, ks[0])
        for k in ks[1:]:
            other = _per_q(run_dir, k)
            for label, getter, pct in (
                ("命中率", lambda r: r["retrieval"].get("hit"), True),
                ("CRAG 分", _crag, False),
            ):
                a = {q: getter(r) for q, r in base.items()}
                b = {q: getter(r) for q, r in other.items()}
                diff, sig = paired_diff(a, b)
                comp_rows.append(
                    [f"{k} 对比 {ks[0]}", label, diff.fmt(pct), "显著" if sig else "无显著差异"]
                )
    if comp_rows:
        out.append(
            (
                "与基线配置的配对比较（差值 = 配置 − 基线）",
                (["比较", "指标", "差值", "结论"], comp_rows),
            )
        )

    tag_rows = []
    for key in keys:
        for tag, v in summary["results"][key]["by_tag"].items():
            tag_rows.append([key, tag, str(v["n"]), _e(v["hit"]), _e(v["crag"], pct=False)])
    out.append(("分题型", (["数据集/平台/配置", "题型", "题数", "命中率@k", "CRAG 分"], tag_rows)))

    attr_rows = []
    for key in keys:
        attr = summary["results"][key]["attribution"]
        attr_rows.append(
            [
                key,
                "、".join(f"{k} {v}" for k, v in sorted(attr.items(), key=lambda x: -x[1])) or "—",
            ]
        )
    out.append(("错题归因", (["数据集/平台/配置", "分布"], attr_rows)))

    lat_rows = []
    for key in keys:
        r = summary["results"][key]
        lat, tok = r["latency_ms"], r.get("tokens") or {}
        lat_rows.append(
            [
                key,
                _num(lat.get("retrieval_p50")),
                _num(lat.get("retrieval_p95")),
                _num(lat.get("generation_p50")),
                _num(lat.get("generation_p95")),
                _num(tok.get("prompt_avg")),
                _num(tok.get("completion_avg")),
            ]
        )
    out.append(
        (
            "耗时与用量",
            (
                [
                    "数据集/平台/配置",
                    "检索 p50（ms）",
                    "检索 p95",
                    "生成 p50（ms）",
                    "生成 p95",
                    "输入 token 均值",
                    "输出 token 均值",
                ],
                lat_rows,
            ),
        )
    )

    # 错题清单
    fail_rows = []
    for key in keys:
        ds_name = key.split("/")[0]
        ds = find_dataset(ds_name, settings.datasets_dirs)
        qmap = {q.id: q for q in ds.questions}
        raw = {
            json.loads(line)["qid"]: json.loads(line)
            for line in (
                run_dir / "raw" / ds_name / f"{key.split('/')[1]}__{key.split('/')[2]}.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not json.loads(line).get("error")
        }
        for qid, row in _per_q(run_dir, key).items():
            if row["attribution"] in (None, "正确"):
                continue
            g = (row.get("generations") or [{}])[0]
            ans = (raw.get(qid, {}).get("generations") or [{}])[0].get("text", "")
            top = (raw.get(qid, {}).get("retrieval", {}).get("chunks") or [{}])[0]
            fail_rows.append(
                [
                    key,
                    qid,
                    qmap[qid].question,
                    row["attribution"],
                    ans[:160],
                    f"{' > '.join(top.get('title_path') or [])}｜{(top.get('text') or '')[:80]}",
                    g.get("judge_reason", ""),
                ]
            )
    if fail_rows:
        out.append(
            (
                "错题清单",
                (
                    ["配置", "题号", "问题", "归因", "回答（节选）", "排第一的段落", "裁判理由"],
                    fail_rows,
                ),
            )
        )
    return out


def _md(sections: list[tuple[str, str | Table]]) -> str:
    lines = ["# 评测报告", ""]
    for title, body in sections:
        lines += [f"## {title}", ""]
        if isinstance(body, str):
            lines += [body, ""]
            continue
        head, rows = body
        esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        lines.append("| " + " | ".join(head) + " |")
        lines.append("|" + " --- |" * len(head))
        lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
        lines.append("")
    return "\n".join(lines)


_CSS = """
body { font: 14px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
       margin: 24px auto; max-width: 1400px; padding: 0 24px; color: #1f2328; }
h1 { font-size: 22px; }
h2 { font-size: 17px; margin-top: 28px; border-bottom: 1px solid #d0d7de; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
th, td { border: 1px solid #d0d7de; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
tr:nth-child(even) td { background: #fbfcfd; }
"""


def _html(sections: list[tuple[str, str | Table]]) -> str:
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>评测报告</title><style>{_CSS}</style></head><body><h1>评测报告</h1>",
    ]
    for title, body in sections:
        parts.append(f"<h2>{html.escape(title)}</h2>")
        if isinstance(body, str):
            parts.append(f"<p>{html.escape(body)}</p>")
            continue
        head, rows = body
        parts.append("<table><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>")
        parts += [
            "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
        ]
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def write_report(run_dir: Path, settings: BenchSettings) -> tuple[Path, Path]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    sections = _sections(run_dir, summary, settings)
    md, page = run_dir / "report.md", run_dir / "report.html"
    md.write_text(_md(sections), encoding="utf-8")
    page.write_text(_html(sections), encoding="utf-8")
    return md, page
