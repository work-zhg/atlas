"""重排（参考 Dify core/rag/rerank/ 与 data_post_processor/reorder.py）。

model_rerank()   交叉编码模型给（查询, 候选）打分，sigmoid 归一到 0~1
weight_rerank()  vector_weight × 余弦 + keyword_weight × 关键词相似度
                 关键词相似度 = 候选集合内部的 TF-IDF 余弦（与 Dify WeightRerankRunner 一致；
                 Dify 注释写的是 BM25，实际算法就是这个）
reorder()        奇数位顺序 + 偶数位倒序，把高分放到上下文首尾（缓解长上下文中间被忽略）
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Sequence

import numpy as np

from .embedding import Reranker
from .keyword import extract_keywords
from .models import Document

__all__ = ["cosine", "model_rerank", "reorder", "weight_rerank"]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0


def _keyword_scores(query: str, documents: list[Document]) -> list[float]:
    query_kw = extract_keywords(query, None)
    docs_kw = [extract_keywords(d.page_content, None) for d in documents]
    total = len(documents)
    vocab = {k for kws in docs_kw for k in kws}
    idf = {
        k: math.log((1 + total) / (1 + sum(1 for kws in docs_kw if k in kws))) + 1 for k in vocab
    }
    q_vec = {k: c * idf.get(k, 0.0) for k, c in Counter(query_kw).items()}

    def _cos(v1: dict[str, float], v2: dict[str, float]) -> float:
        num = sum(v1[k] * v2[k] for k in set(v1) & set(v2))
        den = math.sqrt(sum(x * x for x in v1.values())) * math.sqrt(
            sum(x * x for x in v2.values())
        )
        return num / den if den else 0.0

    return [
        _cos(q_vec, {k: c * idf.get(k, 0.0) for k, c in Counter(kws).items()}) for kws in docs_kw
    ]


def weight_rerank(
    query: str,
    query_vector: list[float],
    documents: list[Document],
    *,
    vector_weight: float,
    keyword_weight: float,
) -> list[Document]:
    """原地写 metadata.score 并按分数降序返回。documents 需带 vector。"""
    if not documents:
        return []
    kw = _keyword_scores(query, documents)
    for doc, k in zip(documents, kw, strict=True):
        v = cosine(query_vector, doc.vector) if doc.vector is not None else 0.0
        doc.metadata["score"] = vector_weight * v + keyword_weight * k
    return sorted(documents, key=lambda d: d.metadata["score"], reverse=True)


async def model_rerank(reranker: Reranker, query: str, documents: list[Document]) -> list[Document]:
    if not documents:
        return []
    scores = await asyncio.to_thread(reranker.rerank, query, [d.page_content for d in documents])
    for doc, s in zip(documents, scores, strict=True):
        doc.metadata["score"] = s
    return sorted(documents, key=lambda d: d.metadata["score"], reverse=True)


def reorder[T](items: list[T]) -> list[T]:
    return items[::2] + items[1::2][::-1]
