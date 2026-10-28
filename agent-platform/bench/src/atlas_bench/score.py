"""判分（可离线重跑）：读运行目录的原始结果 → 逐题分数 + 汇总。

scores/<数据集>/<平台>__<配置>.jsonl   逐题：检索指标、各次生成的判分、错题归因
summary.json                           每个（数据集, 平台, 配置）的指标估计（含置信区间）
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .datasets import find_dataset
from .llm import LlmClient
from .schema import ExperimentConfig, Question
from .scoring.generation import GenerationScorer, attribute
from .scoring.retrieval import score_retrieval
from .settings import BenchSettings
from .stats import estimate

__all__ = ["load_rows", "score_run"]


def load_rows(path: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    """每题取最后一条成功的结果（断点续跑时失败的行会被后来的成功行覆盖）。"""
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["qid"] in wanted and not row.get("error"):
                rows[row["qid"]] = row
    return rows


def _pct(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


async def score_run(
    run_dir: Path, settings: BenchSettings, llm: LlmClient | None
) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cfg = ExperimentConfig.model_validate(manifest["config"])
    scorer = (
        GenerationScorer(
            llm,
            cfg.judge_model,
            cfg.generation_metrics,
            settings.cache_path,
            factual_mode=cfg.factual_correctness_mode,
        )
        if llm is not None and cfg.generation != "none"
        else None
    )
    summary: dict[str, Any] = {"config": manifest["config"], "results": {}}
    sem = asyncio.Semaphore(cfg.concurrency)

    for ds_name, ds_info in manifest["datasets"].items():
        ds = find_dataset(ds_name, settings.datasets_dirs)
        questions: dict[str, Question] = {
            q.id: q for q in ds.questions if q.id in set(ds_info["questions"])
        }
        # ★ 按实验配置里的顺序（第一个配置是配对比较的基线），不按文件名排序
        order = [f"{p.adapter}__{v}.jsonl" for p in cfg.platforms for v in p.variants]
        files = [run_dir / "raw" / ds_name / name for name in order]
        for raw in [f for f in files if f.exists()]:
            platform, variant = raw.stem.split("__", 1)
            rows = load_rows(raw, set(questions))
            retrieval = score_retrieval(
                questions, rows, top_k=cfg.top_k, match=ds.evidence_match
            )

            async def judge(
                qid: str,
                gen: dict[str, Any],
                rows: dict[str, Any] = rows,
                questions: dict[str, Question] = questions,
            ) -> dict[str, Any]:
                contexts = [c["text"] for c in rows[qid]["retrieval"]["chunks"]]
                async with sem:
                    return {
                        "repeat": gen["repeat"],
                        **await scorer.score(questions[qid], gen["text"], contexts),
                    }  # type: ignore[union-attr]

            gen_scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
            if scorer is not None:
                jobs = [(qid, g) for qid, row in rows.items() for g in row.get("generations") or []]
                results = await asyncio.gather(*(judge(qid, g) for qid, g in jobs))
                for (qid, _), res in zip(jobs, results, strict=True):
                    gen_scores[qid].append(res)

            out_path = run_dir / "scores" / ds_name / raw.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            per_q: list[dict[str, Any]] = []
            with out_path.open("w", encoding="utf-8") as f:
                for qid in sorted(rows):
                    q = questions[qid]
                    gens = sorted(gen_scores.get(qid, []), key=lambda g: g["repeat"])
                    item = {
                        "qid": qid,
                        "tags": list(q.tags),
                        "unanswerable": q.unanswerable,
                        "retrieval": retrieval.get(qid, {}),
                        "generations": gens,
                        "attribution": attribute(q, retrieval.get(qid, {}), gens[0])
                        if gens
                        else None,
                    }
                    per_q.append(item)
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            summary["results"][f"{ds_name}/{platform}/{variant}"] = _summarize(per_q, rows)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _mean_over_repeats(  # type: ignore[no-untyped-def]
    gens: list[dict[str, Any]], key: str, fn=None
) -> float | None:
    vals = [fn(g) if fn else g.get(key) for g in gens]
    vals = [v for v in vals if v is not None]
    return float(np.mean(vals)) if vals else None


def _summarize(per_q: list[dict[str, Any]], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with_ev = [p for p in per_q if "hit" in p["retrieval"]]
    answerable = [p for p in per_q if not p["unanswerable"] and p["generations"]]
    unanswerable = [p for p in per_q if p["unanswerable"] and p["generations"]]
    gen_all = [p for p in per_q if p["generations"]]

    def est(items: list[dict[str, Any]], getter) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return asdict(estimate([getter(p) for p in items]))

    out: dict[str, Any] = {
        "n_questions": len(per_q),
        "retrieval": {
            k: est(with_ev, lambda p, k=k: p["retrieval"].get(k))
            for k in ("hit", "recall", "mrr", "ndcg")
        },
        "context_chars": est(per_q, lambda p: p["retrieval"].get("context_chars")),
        "partial_hits": sum(p["retrieval"].get("partial", 0) for p in with_ev),
        "generation": {},
        "by_tag": {},
        "attribution": dict(Counter(p["attribution"] for p in per_q if p["attribution"])),
        "latency_ms": {},
        "tokens": {},
    }
    if gen_all:
        out["generation"] = {
            "crag": est(gen_all, lambda p: _mean_over_repeats(p["generations"], "crag")),
            "accuracy": est(
                answerable,
                lambda p: _mean_over_repeats(
                    p["generations"], "", lambda g: 1.0 if g.get("verdict") == "correct" else 0.0
                ),
            ),
            "refusal_accuracy": est(
                unanswerable,
                lambda p: _mean_over_repeats(
                    p["generations"], "", lambda g: 1.0 if g.get("refused") else 0.0
                ),
            ),
            "false_refusal": est(
                answerable,
                lambda p: _mean_over_repeats(
                    p["generations"], "", lambda g: 1.0 if g.get("refused") else 0.0
                ),
            ),
            "faithfulness": est(
                answerable, lambda p: _mean_over_repeats(p["generations"], "ragas_faithfulness")
            ),
            "factual_correctness": est(
                answerable,
                lambda p: _mean_over_repeats(p["generations"], "ragas_factual_correctness"),
            ),
        }
    tags = sorted({t for p in per_q for t in p["tags"]})
    for t in tags:
        items = [p for p in per_q if t in p["tags"]]
        out["by_tag"][t] = {
            "n": len(items),
            "hit": est(
                [p for p in items if "hit" in p["retrieval"]], lambda p: p["retrieval"].get("hit")
            ),
            "crag": est(
                [p for p in items if p["generations"]],
                lambda p: _mean_over_repeats(p["generations"], "crag"),
            ),
        }
    r_lat = [r["retrieval"]["latency_ms"] for r in rows.values() if r.get("retrieval")]
    g_lat = [g["latency_ms"] for r in rows.values() for g in r.get("generations") or []]
    out["latency_ms"] = {
        "retrieval_p50": _pct(r_lat, 50),
        "retrieval_p95": _pct(r_lat, 95),
        "generation_p50": _pct(g_lat, 50),
        "generation_p95": _pct(g_lat, 95),
    }
    gens = [g for r in rows.values() for g in r.get("generations") or []]
    if gens:
        out["tokens"] = {
            "prompt_avg": float(np.mean([g["prompt_tokens"] for g in gens])),
            "completion_avg": float(np.mean([g["completion_tokens"] for g in gens])),
        }
    return out
