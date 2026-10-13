"""检索指标（doc/rag-eval-design.html §9）。

    ① 判定（自研，matching.py）：每条标准出处 → 最早命中它的段落排名
    ② 计算（ranx，口径与 BEIR / C-MTEB 用的 pytrec_eval 一致）：命中率@k、召回率@k、MRR@k、nDCG@k

ranx 需要「排好序的文档」：第 r 名段落命中了出处 i，就记为文档 e{i}；没命中任何出处的段落记为
m{r}（不相关文档，占住位置，nDCG 才算得对）。同一出处只在最早命中的位置计一次。
没有出处的题（拒答题、摘要题）不参与检索指标。
"""

from __future__ import annotations

from typing import Any

from ranx import Qrels, Run, evaluate

from ..schema import Question
from .matching import match_evidence

__all__ = ["score_retrieval"]


def _metric_names(k: int) -> dict[str, str]:
    return {"hit": f"hit_rate@{k}", "recall": f"recall@{k}", "mrr": f"mrr@{k}", "ndcg": f"ndcg@{k}"}


def score_retrieval(
    questions: dict[str, Question], rows: dict[str, dict[str, Any]], *, top_k: int
) -> dict[str, dict[str, Any]]:
    """→ {qid: {hit, recall, mrr, ndcg, context_chars, partial, first_rank, matches}}。"""
    per_q: dict[str, dict[str, Any]] = {}
    qrels: dict[str, dict[str, int]] = {}
    run: dict[str, dict[str, float]] = {}

    for qid, row in rows.items():
        q = questions[qid]
        chunks = (row.get("retrieval") or {}).get("chunks") or []
        texts = [c["text"] for c in chunks[:top_k]]
        item: dict[str, Any] = {"context_chars": sum(len(t) for t in texts)}
        quotes = [e.quote for e in q.evidence if e.quote]
        if not quotes:
            per_q[qid] = item
            continue
        matches = match_evidence(quotes, texts)
        qrels[qid] = {f"e{m.evidence_index}": 1 for m in matches}
        ranked: dict[str, float] = {}
        hit_at: dict[int, list[int]] = {}
        for m in matches:
            if m.rank is not None:
                hit_at.setdefault(m.rank, []).append(m.evidence_index)
        for r in range(1, len(texts) + 1):
            ids = hit_at.get(r) or []
            if ids:
                for j, ev in enumerate(ids):
                    ranked[f"e{ev}"] = float(len(texts) - r + 1) - j * 1e-3
            else:
                ranked[f"m{r}"] = float(len(texts) - r + 1)
        run[qid] = ranked
        ranks = [m.rank for m in matches if m.rank is not None]
        item.update(
            first_rank=min(ranks) if ranks else None,
            partial=sum(1 for m in matches if m.kind == "partial"),
            matches=[
                {"evidence": m.evidence_index, "rank": m.rank, "kind": m.kind} for m in matches
            ],
        )
        per_q[qid] = item

    if qrels:
        names = _metric_names(top_k)
        # ranx 对没有任何检索结果的题要求 run 里也有这个题：放一个空的不相关文档
        for qid in qrels:
            run.setdefault(qid, {"m0": 0.0})
        scores = evaluate(
            Qrels(qrels), Run(run), list(names.values()), return_mean=False, make_comparable=True
        )
        order = list(Run(run).keys())
        for key, metric in names.items():
            values = scores[metric] if len(names) > 1 else scores
            for qid, v in zip(order, values, strict=True):
                per_q[qid][key] = float(v)
    return per_q
