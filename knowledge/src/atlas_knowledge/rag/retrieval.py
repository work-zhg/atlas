"""检索（参考 Dify datasource/retrieval_service.py 与 retrieval/dataset_retrieval.py）。

单库 RetrievalService.retrieve：
    semantic_search    查询向量 → Qdrant 稠密检索
    full_text_search   jieba 分词 → Qdrant BM25 稀疏检索
    hybrid_search      两路并行 → 按 point 去重 → 重排（必须）
    → 重排（模型 / 加权，可选）→ 分数阈值 → top_k
    → 组装：子块命中回到父分段（分段分数 = 命中子块的最高分），附命中子块

多库 multiple_retrieve：各库依次召回（每库按自己的检索方式）→ 合并 → 统一重排（可选）→ top_k。
单路召回（大模型路由）是 P2。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildChunk, Dataset, Segment
from ..db.models import Document as DocRow
from ..errors import Invalid
from .embedding import CacheEmbedding, ModelRegistry
from .keyword import bm25_query
from .models import Document
from .rerank import model_rerank, weight_rerank
from .vdb import Hit, VectorFactory

__all__ = [
    "DEFAULT_RETRIEVAL_CONFIG",
    "RetrievalService",
    "RetrievedSegment",
    "normalize_retrieval_config",
]

logger = logging.getLogger(__name__)

SEARCH_METHODS = ("semantic_search", "full_text_search", "hybrid_search")

#: 同 Dify 的默认检索设置
DEFAULT_RETRIEVAL_CONFIG: dict[str, Any] = {
    "search_method": "semantic_search",
    "top_k": 4,
    "score_threshold_enabled": False,
    "score_threshold": 0.5,
    "reranking_enable": False,
    "reranking_mode": "weighted_score",
    "reranking_model": {"provider": "fastembed", "model": ""},
    "weights": {
        "vector_setting": {"vector_weight": 0.7},
        "keyword_setting": {"keyword_weight": 0.3},
    },
}


def normalize_retrieval_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_RETRIEVAL_CONFIG)
    for key, value in (raw or {}).items():
        if key in cfg and value is not None:
            cfg[key] = value
    if cfg["search_method"] not in SEARCH_METHODS:
        raise Invalid(f"不支持的检索方式：{cfg['search_method']}")
    try:
        cfg["top_k"] = int(cfg["top_k"])
        cfg["score_threshold"] = float(cfg["score_threshold"])
    except (TypeError, ValueError) as exc:
        raise Invalid("top_k / score_threshold 格式错误") from exc
    if not 1 <= cfg["top_k"] <= 20:
        raise Invalid("top_k 须在 1 ~ 20 之间")
    if not 0 <= cfg["score_threshold"] <= 1:
        raise Invalid("score_threshold 须在 0 ~ 1 之间")
    if cfg["reranking_mode"] not in ("reranking_model", "weighted_score"):
        raise Invalid("reranking_mode 只能是 reranking_model 或 weighted_score")
    # ★ 混合检索必须重排：两路分数（余弦 / BM25）不可直接比较（同 Dify）
    if cfg["search_method"] == "hybrid_search":
        cfg["reranking_enable"] = True
    if cfg["reranking_enable"] and cfg["reranking_mode"] == "weighted_score":
        try:
            vw = float(cfg["weights"]["vector_setting"]["vector_weight"])
            kw = float(cfg["weights"]["keyword_setting"]["keyword_weight"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Invalid("加权重排需要 weights.vector_setting / keyword_setting") from exc
        if vw < 0 or kw < 0 or abs(vw + kw - 1.0) > 1e-6:
            raise Invalid("向量权重与关键词权重之和须为 1")
    return cfg


@dataclass
class RetrievedSegment:
    dataset_uuid: str
    dataset_name: str
    document_uuid: str
    document_name: str
    segment_uuid: str
    position: int
    content: str
    score: float
    child_chunks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_uuid": self.dataset_uuid,
            "dataset_name": self.dataset_name,
            "document_uuid": self.document_uuid,
            "document_name": self.document_name,
            "segment_uuid": self.segment_uuid,
            "position": self.position,
            "content": self.content,
            "score": round(self.score, 6),
            "child_chunks": self.child_chunks,
        }


class RetrievalService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        registry: ModelRegistry,
        vectors: VectorFactory,
        redis: Any = None,
        query_ttl_s: int = 600,
    ) -> None:
        self._s = session
        self._registry = registry
        self._vectors = vectors
        self._redis = redis
        self._ttl = query_ttl_s

    def _embedder(self, ds: Dataset) -> CacheEmbedding:
        model = self._registry.embedding(ds.embedding_provider, ds.embedding_model)
        return CacheEmbedding(model, redis=self._redis, query_ttl_s=self._ttl)

    # ------------------------------------------------------------------ 单库

    async def _candidates(
        self, ds: Dataset, query: str, cfg: dict[str, Any]
    ) -> tuple[list[Document], list[float] | None]:
        """召回候选（point 级），返回 (候选, 查询向量)。"""
        store = self._vectors.for_collection(ds.collection_name)
        method = cfg["search_method"]
        top_k = cfg["top_k"]
        embedder = self._embedder(ds)
        qvec: list[float] | None = None
        # 语义检索单独使用、不重排时，阈值直接交给向量库
        threshold = (
            cfg["score_threshold"]
            if cfg["score_threshold_enabled"] and not cfg["reranking_enable"]
            else None
        )

        async def _semantic() -> list[Hit]:
            nonlocal qvec
            qvec = await embedder.embed_query(query)
            return await store.search_by_vector(qvec, top_k=top_k, score_threshold=threshold)

        async def _full_text() -> list[Hit]:
            indices, values = bm25_query(query)
            return await store.search_by_full_text(indices, values, top_k=top_k)

        if method == "semantic_search":
            hits = await _semantic()
        elif method == "full_text_search":
            hits = await _full_text()
        else:
            sem, ft = await asyncio.gather(_semantic(), _full_text())
            seen: set[str] = set()
            hits = []
            for h in [*sem, *ft]:
                if h.id not in seen:
                    seen.add(h.id)
                    hits.append(h)

        contents = await self._point_contents(hits)
        docs: list[Document] = []
        for h in hits:
            text = contents.get(h.id)
            if text is None:  # 库里已删、向量库还没清的残留
                continue
            docs.append(
                Document(
                    page_content=text,
                    metadata={**h.payload, "point_id": h.id, "score": h.score},
                    vector=h.vector,
                )
            )

        if cfg["reranking_enable"] and docs:
            if cfg["reranking_mode"] == "reranking_model":
                docs = await self._model_rerank(cfg, query, docs)
            else:
                if qvec is None:
                    qvec = await embedder.embed_query(query)
                w = cfg["weights"]
                docs = weight_rerank(
                    query,
                    qvec,
                    docs,
                    vector_weight=float(w["vector_setting"]["vector_weight"]),
                    keyword_weight=float(w["keyword_setting"]["keyword_weight"]),
                )
            if cfg["score_threshold_enabled"]:
                docs = [d for d in docs if d.metadata["score"] >= cfg["score_threshold"]]
        return docs, qvec

    async def _model_rerank(
        self, cfg: dict[str, Any], query: str, docs: list[Document]
    ) -> list[Document]:
        model = (cfg.get("reranking_model") or {}).get("model") or ""
        if not model:
            raise Invalid("重排模型未配置")
        return await model_rerank(self._registry.reranker(model), query, docs)

    async def _point_contents(self, hits: list[Hit]) -> dict[str, str]:
        child_ids = [UUID(h.id) for h in hits if h.payload.get("kind") == "child"]
        seg_ids = [UUID(h.id) for h in hits if h.payload.get("kind") != "child"]
        out: dict[str, str] = {}
        if child_ids:
            rows = await self._s.execute(
                select(ChildChunk.uuid, ChildChunk.content).where(ChildChunk.uuid.in_(child_ids))
            )
            out.update({str(u): c for u, c in rows.all()})
        if seg_ids:
            rows = await self._s.execute(
                select(Segment.uuid, Segment.content).where(Segment.uuid.in_(seg_ids))
            )
            out.update({str(u): c for u, c in rows.all()})
        return out

    async def _assemble(
        self, datasets: dict[str, Dataset], docs: list[Document]
    ) -> list[RetrievedSegment]:
        """point 级候选 → 分段级结果（保持分数顺序）。"""
        seg_ids = list(dict.fromkeys(UUID(d.metadata["segment_uuid"]) for d in docs))
        if not seg_ids:
            return []
        segs = {
            str(s.uuid): s
            for s in (await self._s.scalars(select(Segment).where(Segment.uuid.in_(seg_ids)))).all()
        }
        doc_ids = {s.document_uuid for s in segs.values()}
        names = {
            str(u): n
            for u, n in (
                await self._s.execute(
                    select(DocRow.uuid, DocRow.name).where(DocRow.uuid.in_(doc_ids))
                )
            ).all()
        }
        out: dict[str, RetrievedSegment] = {}
        for d in docs:
            sid = d.metadata["segment_uuid"]
            seg = segs.get(sid)
            if seg is None:
                continue
            score = float(d.metadata["score"])
            item = out.get(sid)
            if item is None:
                ds = datasets[str(seg.dataset_uuid)]
                item = RetrievedSegment(
                    dataset_uuid=str(seg.dataset_uuid),
                    dataset_name=ds.name,
                    document_uuid=str(seg.document_uuid),
                    document_name=names.get(str(seg.document_uuid), ""),
                    segment_uuid=sid,
                    position=seg.position,
                    content=seg.content,
                    score=score,
                )
                out[sid] = item
            else:
                item.score = max(item.score, score)
            if d.metadata.get("kind") == "child":
                item.child_chunks.append({"content": d.page_content, "score": round(score, 6)})
        return sorted(out.values(), key=lambda r: r.score, reverse=True)

    async def retrieve(
        self, ds: Dataset, query: str, cfg: dict[str, Any] | None = None
    ) -> list[RetrievedSegment]:
        cfg = normalize_retrieval_config(cfg if cfg is not None else ds.retrieval_config)
        docs, _ = await self._candidates(ds, query, cfg)
        result = await self._assemble({str(ds.uuid): ds}, docs)
        result = result[: cfg["top_k"]]
        await self._count_hits(result)
        return result

    # ------------------------------------------------------------------ 多库

    async def multiple_retrieve(
        self, datasets: list[Dataset], query: str, cfg: dict[str, Any]
    ) -> list[RetrievedSegment]:
        """各库按自己的检索方式召回，再按 cfg（top_k / 阈值 / 重排）统一收口。"""
        cfg = normalize_retrieval_config(cfg)
        if not datasets:
            return []
        if len(datasets) == 1:
            ds = datasets[0]
            own = {**normalize_retrieval_config(ds.retrieval_config), **_overrides(cfg)}
            return await self.retrieve(ds, query, own)

        per_ds: list[list[Document]] = []
        for ds in datasets:
            # ★ 不并发：同一个 AsyncSession 不能并发执行查询。库数一般个位数，串行可接受
            own = normalize_retrieval_config(ds.retrieval_config)
            own["top_k"] = cfg["top_k"]
            docs, _ = await self._candidates(ds, query, own)
            per_ds.append(docs)
        merged = [d for docs in per_ds for d in docs]

        if cfg["reranking_enable"] and merged:
            if cfg["reranking_mode"] == "reranking_model":
                merged = await self._model_rerank(cfg, query, merged)
            else:
                models = {(d.embedding_provider, d.embedding_model) for d in datasets}
                if len(models) > 1:
                    raise Invalid("加权重排要求各知识库使用同一个向量模型；请改用重排模型")
                qvec = await self._embedder(datasets[0]).embed_query(query)
                w = cfg["weights"]
                merged = weight_rerank(
                    query,
                    qvec,
                    merged,
                    vector_weight=float(w["vector_setting"]["vector_weight"]),
                    keyword_weight=float(w["keyword_setting"]["keyword_weight"]),
                )
            if cfg["score_threshold_enabled"]:
                merged = [d for d in merged if d.metadata["score"] >= cfg["score_threshold"]]
        else:
            merged.sort(key=lambda d: d.metadata["score"], reverse=True)

        result = await self._assemble({str(d.uuid): d for d in datasets}, merged)
        result = result[: cfg["top_k"]]
        await self._count_hits(result)
        return result

    async def _count_hits(self, result: list[RetrievedSegment]) -> None:
        if result:
            await self._s.execute(
                update(Segment)
                .where(Segment.uuid.in_([UUID(r.segment_uuid) for r in result]))
                .values(hit_count=Segment.hit_count + 1)
            )


def _overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """多库配置里覆盖到单库的部分：top_k 与阈值；重排只在显式开启时覆盖。"""
    out = {
        "top_k": cfg["top_k"],
        "score_threshold_enabled": cfg["score_threshold_enabled"],
        "score_threshold": cfg["score_threshold"],
    }
    if cfg["reranking_enable"]:
        out.update(
            reranking_enable=True,
            reranking_mode=cfg["reranking_mode"],
            reranking_model=cfg["reranking_model"],
            weights=cfg["weights"],
        )
    return out
