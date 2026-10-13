"""检索（参考 Dify datasource/retrieval_service.py 与 retrieval/dataset_retrieval.py）。

单库 RetrievalService.retrieve：
    semantic_search    查询向量 → Qdrant 稠密检索
    full_text_search   jieba 分词 → Qdrant BM25 稀疏检索
    hybrid_search      两路并行 → 按 point 去重 → 重排（必须）
    → 重排（模型 / 加权，可选）→ 分数阈值 → top_k
    → 组装：子块命中回到父分段（分段分数 = 命中子块的最高分），附命中子块

多库 multiple_retrieve：各库依次召回（每库按自己的检索方式）→ 合并 → 统一重排（可选）→ top_k。
单路召回（大模型路由）是 P2。

每个环节经 RetrievalRecorder 记下候选并开 span（rag/recorder.py）：
    semantic / full_text → merged（混合去重）→ rerank → threshold → assemble → final
多库时各库的环节带 dataset_uuid，再加跨库的 merged / rerank / threshold。
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
from ..indexing.loader import embed_text
from .embedding import CacheEmbedding, ModelRegistry
from .keyword import bm25_query
from .models import Document
from .recorder import RetrievalRecorder
from .rerank import model_rerank, weight_rerank
from .structure import path_text
from .vdb import Hit, VectorFactory

__all__ = [
    "DEFAULT_RETRIEVAL_CONFIG",
    "RetrievalService",
    "RetrievedSegment",
    "normalize_retrieval_config",
]

logger = logging.getLogger(__name__)

SEARCH_METHODS = ("semantic_search", "full_text_search", "hybrid_search")
#: 父子分段时子块候选数 = top_k × 它
_CHILD_FANOUT = 4

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
    title_path: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_uuid": self.dataset_uuid,
            "dataset_name": self.dataset_name,
            "document_uuid": self.document_uuid,
            "document_name": self.document_name,
            "segment_uuid": self.segment_uuid,
            "position": self.position,
            "content": self.content,
            "title_path": self.title_path,
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
        self, ds: Dataset, query: str, cfg: dict[str, Any], rec: RetrievalRecorder
    ) -> tuple[list[Document], list[float] | None]:
        """召回候选（point 级），返回 (候选, 查询向量)。"""
        dsu = str(ds.uuid)
        store = self._vectors.for_collection(ds.collection_name)
        method = cfg["search_method"]
        # ★ 父子分段的命中单位是子块，而 top_k 是按父块算的：同一父块的几个子块会在组装时去重，
        #   只取 top_k 个子块常常凑不满 top_k 个父块。多取一些候选，组装后再截断
        top_k = cfg["top_k"] * (_CHILD_FANOUT if ds.doc_form == "hierarchical_model" else 1)
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
            # 查询向量是语义召回的一部分：嵌在它下面（也保证两路并发时环节顺序稳定）
            with rec.stage(
                "semantic",
                dataset_uuid=dsu,
                span_kind="RETRIEVER",
                top_k=top_k,
                vdb_threshold=threshold,
            ) as st:
                with rec.stage("embed_query", dataset_uuid=dsu, span_kind="EMBEDDING"):
                    qvec = await embedder.embed_query(query)
                hits = await store.search_by_vector(qvec, top_k=top_k, score_threshold=threshold)
                st.set(hits)
            return hits

        async def _full_text() -> list[Hit]:
            with rec.stage("full_text", dataset_uuid=dsu, span_kind="RETRIEVER", top_k=top_k) as st:
                indices, values = bm25_query(query)
                hits = await store.search_by_full_text(indices, values, top_k=top_k)
                st.set(hits, query_terms=len(indices))
            return hits

        if method == "semantic_search":
            hits = await _semantic()
        elif method == "full_text_search":
            hits = await _full_text()
        else:
            sem, ft = await asyncio.gather(_semantic(), _full_text())
            with rec.stage("merged", dataset_uuid=dsu) as st:
                seen: set[str] = set()
                hits = []
                for h in [*sem, *ft]:
                    if h.id not in seen:
                        seen.add(h.id)
                        hits.append(h)
                st.set(hits)

        contents = await self._point_contents(hits)
        docs: list[Document] = []
        for h in hits:
            found = contents.get(h.id)
            if found is None:  # 库里已删、向量库还没清的残留
                continue
            raw, embed = found
            # ★ 重排用「前缀 + 原文」打分（带标题路径更准）；返回给前端与模型的用原文
            docs.append(
                Document(
                    page_content=embed,
                    metadata={**h.payload, "point_id": h.id, "score": h.score, "raw": raw},
                    vector=h.vector,
                )
            )

        if cfg["reranking_enable"] and docs:
            if cfg["reranking_mode"] == "weighted_score" and qvec is None:
                with rec.stage("embed_query", dataset_uuid=dsu, span_kind="EMBEDDING"):
                    qvec = await embedder.embed_query(query)
            docs = await self._rerank(cfg, query, docs, qvec, rec, dataset_uuid=dsu)
            docs = self._threshold(cfg, docs, rec, dataset_uuid=dsu)
        return docs, qvec

    async def _rerank(
        self,
        cfg: dict[str, Any],
        query: str,
        docs: list[Document],
        qvec: list[float] | None,
        rec: RetrievalRecorder,
        *,
        dataset_uuid: str | None = None,
    ) -> list[Document]:
        mode = cfg["reranking_mode"]
        model = (cfg.get("reranking_model") or {}).get("model") or None
        with rec.stage(
            "rerank",
            dataset_uuid=dataset_uuid,
            span_kind="RERANKER",
            mode=mode,
            model=model if mode == "reranking_model" else None,
        ) as st:
            if mode == "reranking_model":
                docs = await self._model_rerank(cfg, query, docs)
            else:
                assert qvec is not None
                w = cfg["weights"]
                docs = weight_rerank(
                    query,
                    qvec,
                    docs,
                    vector_weight=float(w["vector_setting"]["vector_weight"]),
                    keyword_weight=float(w["keyword_setting"]["keyword_weight"]),
                )
            st.set(docs)
        return docs

    @staticmethod
    def _threshold(
        cfg: dict[str, Any],
        docs: list[Document],
        rec: RetrievalRecorder,
        *,
        dataset_uuid: str | None = None,
    ) -> list[Document]:
        if not cfg["score_threshold_enabled"]:
            return docs
        with rec.stage(
            "threshold", dataset_uuid=dataset_uuid, threshold=cfg["score_threshold"]
        ) as st:
            docs = [d for d in docs if d.metadata["score"] >= cfg["score_threshold"]]
            st.set(docs)
        return docs

    async def _model_rerank(
        self, cfg: dict[str, Any], query: str, docs: list[Document]
    ) -> list[Document]:
        model = (cfg.get("reranking_model") or {}).get("model") or ""
        if not model:
            raise Invalid("重排模型未配置")
        return await model_rerank(self._registry.reranker(model), query, docs)

    async def _point_contents(self, hits: list[Hit]) -> dict[str, tuple[str, str]]:
        """point id → (原文, 建向量用的文本)。"""
        child_ids = [UUID(h.id) for h in hits if h.payload.get("kind") == "child"]
        seg_ids = [UUID(h.id) for h in hits if h.payload.get("kind") != "child"]
        out: dict[str, tuple[str, str]] = {}
        if child_ids:
            rows = await self._s.execute(
                select(ChildChunk.uuid, ChildChunk.content, ChildChunk.context).where(
                    ChildChunk.uuid.in_(child_ids)
                )
            )
            out.update({str(u): (c, embed_text(c, ctx)) for u, c, ctx in rows.all()})
        if seg_ids:
            rows = await self._s.execute(
                select(Segment.uuid, Segment.content, Segment.title_path).where(
                    Segment.uuid.in_(seg_ids)
                )
            )
            out.update({str(u): (c, embed_text(c, path_text(p))) for u, c, p in rows.all()})
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
                    title_path=list(seg.title_path or []),
                )
                out[sid] = item
            else:
                item.score = max(item.score, score)
            if d.metadata.get("kind") == "child":
                item.child_chunks.append(
                    {"content": d.metadata.get("raw", d.page_content), "score": round(score, 6)}
                )
        return sorted(out.values(), key=lambda r: r.score, reverse=True)

    async def _finish(
        self,
        datasets: dict[str, Dataset],
        docs: list[Document],
        top_k: int,
        rec: RetrievalRecorder,
    ) -> list[RetrievedSegment]:
        with rec.stage("assemble") as st:
            result = await self._assemble(datasets, docs)
            st.set(result)
        with rec.stage("final", top_k=top_k) as st:
            result = result[:top_k]
            st.set(result)
        await self._count_hits(result)
        return result

    async def retrieve(
        self,
        ds: Dataset,
        query: str,
        cfg: dict[str, Any] | None = None,
        rec: RetrievalRecorder | None = None,
    ) -> list[RetrievedSegment]:
        rec = rec or RetrievalRecorder()
        cfg = normalize_retrieval_config(cfg if cfg is not None else ds.retrieval_config)
        docs, _ = await self._candidates(ds, query, cfg, rec)
        return await self._finish({str(ds.uuid): ds}, docs, cfg["top_k"], rec)

    # ------------------------------------------------------------------ 多库

    async def multiple_retrieve(
        self,
        datasets: list[Dataset],
        query: str,
        cfg: dict[str, Any],
        rec: RetrievalRecorder | None = None,
    ) -> list[RetrievedSegment]:
        """各库按自己的检索方式召回，再按 cfg（top_k / 阈值 / 重排）统一收口。"""
        rec = rec or RetrievalRecorder()
        cfg = normalize_retrieval_config(cfg)
        if not datasets:
            return []
        if len(datasets) == 1:
            ds = datasets[0]
            own = {**normalize_retrieval_config(ds.retrieval_config), **_overrides(cfg)}
            return await self.retrieve(ds, query, own, rec)

        per_ds: list[list[Document]] = []
        for ds in datasets:
            # ★ 不并发：同一个 AsyncSession 不能并发执行查询。库数一般个位数，串行可接受
            own = normalize_retrieval_config(ds.retrieval_config)
            own["top_k"] = cfg["top_k"]
            docs, _ = await self._candidates(ds, query, own, rec)
            per_ds.append(docs)
        with rec.stage("merged") as st:
            merged = [d for docs in per_ds for d in docs]
            if not cfg["reranking_enable"]:
                merged.sort(key=lambda d: d.metadata["score"], reverse=True)
            st.set(merged)

        if cfg["reranking_enable"] and merged:
            qvec: list[float] | None = None
            if cfg["reranking_mode"] == "weighted_score":
                models = {(d.embedding_provider, d.embedding_model) for d in datasets}
                if len(models) > 1:
                    raise Invalid("加权重排要求各知识库使用同一个向量模型；请改用重排模型")
                with rec.stage("embed_query", span_kind="EMBEDDING"):
                    qvec = await self._embedder(datasets[0]).embed_query(query)
            merged = await self._rerank(cfg, query, merged, qvec, rec)
            merged = self._threshold(cfg, merged, rec)

        return await self._finish({str(d.uuid): d for d in datasets}, merged, cfg["top_k"], rec)

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
