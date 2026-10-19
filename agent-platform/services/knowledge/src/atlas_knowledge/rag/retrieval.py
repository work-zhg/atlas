"""检索（参考 Dify datasource/retrieval_service.py 与 retrieval/dataset_retrieval.py）。

单库 RetrievalService.retrieve：
    semantic_search    查询向量 → Qdrant 稠密检索
    full_text_search   jieba 分词 → Qdrant BM25 稀疏检索
    hybrid_search      两路并行 → 按 point 去重 → 重排（必须）
    → 重排（模型 / 加权，可选）→ 分数阈值 → top_k
    → 组装：子块命中回到父分段（分段分数 = 命中子块的最高分），附命中子块

多库 multiple_retrieve：各库依次召回（每库按自己的检索方式）→ 合并 → 统一重排（可选）→ top_k。

企业级扩展（doc/knowledge-enterprise-design.html §7）：
    查询理解（rag/query.py）给出 QueryPlan：多个表述（改写、多查询、HyDE）各自召回，
    RRF（倒数排名融合）合并后再重排；复合问题拆成子问题各走一遍，结果按子问题分组、再 RRF 合并
    检索范围（rag/filters.py）：文档级权限、版本与时效、元数据过滤，作为召回的附加过滤
    多样性：MMR（重排后、组装前，point 级）→ 每文档上限 → 相邻分段合并（组装后，分段级）

每个环节经 RetrievalRecorder 记下候选并开 span（rag/recorder.py）：
    query_understanding → semantic / full_text（各表述）→ merged（混合去重 / RRF）→ rerank
    → threshold → diversity → assemble → final
多库时各库的环节带 dataset_uuid，再加跨库的 merged / rerank / threshold。
子问题的环节带 branch（子问题序号）。
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
from ..indexing.loader import CHILD_KINDS, embed_text
from .embedding import CacheEmbedding, ModelRegistry
from .keyword import bm25_query
from .models import Document
from .query import QueryPlan
from .recorder import RetrievalRecorder
from .rerank import cosine, model_rerank, weight_rerank
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
    # ── 查询理解（§7.1）。对话改写在有历史且问题含指代 / 过短时自动做，不用开关
    "multi_query": False,
    #: 自适应多查询：首轮最高分低于它时自动生成多个表述再检索一次；0 = 关
    "adaptive_multi_query_threshold": 0.0,
    "hyde": False,
    "decompose": False,
    #: 库里定义了允许抽取的元数据字段时，从问题里抽过滤条件
    "filter_extraction": True,
    #: 绑定的库超过这个数才做多库路由
    "routing_threshold": 5,
    # ── 结果多样性（§7.3）
    #: 同一文档最多返回几个分段；0 = 不限
    "max_per_document": 0,
    "mmr_enable": False,
    #: MMR 的 λ：越大越偏相关性，越小越偏多样性
    "mmr_lambda": 0.7,
    #: 同一文档里位置相邻、都命中的分段合并成一个结果
    "merge_adjacent": False,
}

_RRF_K = 60
_BOOLS = ("multi_query", "hyde", "decompose", "filter_extraction", "mmr_enable", "merge_adjacent")


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
    for key in _BOOLS:
        cfg[key] = bool(cfg[key])
    try:
        cfg["max_per_document"] = int(cfg["max_per_document"])
        cfg["mmr_lambda"] = float(cfg["mmr_lambda"])
        cfg["routing_threshold"] = int(cfg["routing_threshold"])
        cfg["adaptive_multi_query_threshold"] = float(cfg["adaptive_multi_query_threshold"])
    except (TypeError, ValueError) as exc:
        raise Invalid("max_per_document / mmr_lambda / routing_threshold 格式错误") from exc
    if not 0 <= cfg["adaptive_multi_query_threshold"] <= 1:
        raise Invalid("adaptive_multi_query_threshold 须在 0 ~ 1 之间")
    if cfg["max_per_document"] < 0 or not 0 <= cfg["mmr_lambda"] <= 1:
        raise Invalid("max_per_document 须 ≥ 0，mmr_lambda 须在 0 ~ 1 之间")
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
    #: 引用定位：{pages, boxes, assets}（DocIR 入库的文档才有）
    source_locator: dict[str, Any] | None = None
    #: 相邻合并进来的其他分段（§7.3）
    merged_segments: list[str] = field(default_factory=list)
    #: 文档版本与时效（§7.5）：{is_current, valid_from, valid_to}
    document_version: dict[str, Any] | None = None
    #: 相邻合并后的末位置
    position_end: int | None = None
    #: 复合问题时命中的子问题序号
    sub_question: int | None = None

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
            "source_locator": self.source_locator,
            "merged_segments": self.merged_segments,
            "document_version": self.document_version,
            "sub_question": self.sub_question,
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
        self,
        ds: Dataset,
        query: str,
        cfg: dict[str, Any],
        rec: RetrievalRecorder,
        *,
        conditions: Any = None,
        fuse: bool = False,
        **labels: Any,
    ) -> tuple[list[Document], list[float] | None]:
        """召回候选（point 级），返回 (候选, 查询向量)。

        conditions：检索范围（权限、版本时效、元数据）的附加过滤。
        fuse：多表述召回的一路（不重排）：混合检索两路用 RRF 合并，排名才有意义。
        labels：写进各环节记录（表述序号 variant、子问题 branch）。
        """
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
                **labels,
            ) as st:
                with rec.stage("embed_query", dataset_uuid=dsu, span_kind="EMBEDDING"):
                    qvec = await embedder.embed_query(query)
                hits = await store.search_by_vector(
                    qvec, top_k=top_k, score_threshold=threshold, conditions=conditions
                )
                st.set(hits)
            return hits

        async def _full_text() -> list[Hit]:
            with rec.stage(
                "full_text", dataset_uuid=dsu, span_kind="RETRIEVER", top_k=top_k, **labels
            ) as st:
                indices, values = bm25_query(query)
                hits = await store.search_by_full_text(
                    indices, values, top_k=top_k, conditions=conditions
                )
                st.set(hits, query_terms=len(indices))
            return hits

        if method == "semantic_search":
            hits = await _semantic()
        elif method == "full_text_search":
            hits = await _full_text()
        else:
            sem, ft = await asyncio.gather(_semantic(), _full_text())
            with rec.stage(
                "merged", dataset_uuid=dsu, method="rrf" if fuse else None, **labels
            ) as st:
                if fuse:
                    hits = rrf([sem, ft], key=lambda h: h.id)
                else:
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
            docs = await self._rerank(cfg, query, docs, qvec, rec, dataset_uuid=dsu, **labels)
            docs = self._threshold(cfg, docs, rec, dataset_uuid=dsu, **labels)
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
        **labels: Any,
    ) -> list[Document]:
        mode = cfg["reranking_mode"]
        model = (cfg.get("reranking_model") or {}).get("model") or None
        with rec.stage(
            "rerank",
            dataset_uuid=dataset_uuid,
            span_kind="RERANKER",
            mode=mode,
            model=model if mode == "reranking_model" else None,
            **labels,
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
        **labels: Any,
    ) -> list[Document]:
        if not cfg["score_threshold_enabled"]:
            return docs
        with rec.stage(
            "threshold", dataset_uuid=dataset_uuid, threshold=cfg["score_threshold"], **labels
        ) as st:
            docs = [d for d in docs if d.metadata["score"] >= cfg["score_threshold"]]
            st.set(docs)
        return docs

    async def _model_rerank(
        self, cfg: dict[str, Any], query: str, docs: list[Document]
    ) -> list[Document]:
        spec = cfg.get("reranking_model") or {}
        model = spec.get("model") or ""
        if not model:
            raise Invalid("重排模型未配置")
        provider = spec.get("provider") or "fastembed"
        reranker = await asyncio.to_thread(self._registry.reranker, model, provider)
        return await model_rerank(reranker, query, docs)

    async def _point_contents(self, hits: list[Hit]) -> dict[str, tuple[str, str]]:
        """point id → (原文, 建向量用的文本)。"""
        child_ids = [UUID(h.id) for h in hits if h.payload.get("kind") in CHILD_KINDS]
        seg_ids = [UUID(h.id) for h in hits if h.payload.get("kind") not in CHILD_KINDS]
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
                select(Segment.uuid, Segment.content, Segment.title_path, Segment.context).where(
                    Segment.uuid.in_(seg_ids)
                )
            )
            out.update(
                {
                    str(u): (c, embed_text(c, ctx if ctx is not None else path_text(p)))
                    for u, c, p, ctx in rows.all()
                }
            )
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
        rows = (
            await self._s.execute(
                select(
                    DocRow.uuid, DocRow.name, DocRow.is_current, DocRow.valid_from, DocRow.valid_to
                ).where(DocRow.uuid.in_(doc_ids))
            )
        ).all()
        names = {str(r[0]): r[1] for r in rows}
        versions = {
            str(r[0]): {
                "is_current": r[2],
                "valid_from": r[3].isoformat() if r[3] else None,
                "valid_to": r[4].isoformat() if r[4] else None,
            }
            for r in rows
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
                    source_locator=seg.source_locator,
                    document_version=versions.get(str(seg.document_uuid)),
                )
                out[sid] = item
            else:
                item.score = max(item.score, score)
            if d.metadata.get("kind") in CHILD_KINDS:
                item.child_chunks.append(
                    {
                        "content": d.metadata.get("raw", d.page_content),
                        "score": round(score, 6),
                        "kind": d.metadata.get("kind"),
                    }
                )
        return sorted(out.values(), key=lambda r: r.score, reverse=True)

    async def _recall(
        self,
        ds: Dataset,
        variants: list[tuple[str, str | None]],
        query: str,
        cfg: dict[str, Any],
        rec: RetrievalRecorder,
        *,
        conditions: Any = None,
        **labels: Any,
    ) -> list[Document]:
        """一个库：一个表述走原流程（召回 → 重排 → 阈值）；多个表述各自召回、RRF 合并后再重排。"""
        if len(variants) <= 1:
            text = variants[0][0] if variants else query
            docs, _ = await self._candidates(ds, text, cfg, rec, conditions=conditions, **labels)
            return docs
        dsu = str(ds.uuid)
        inner = {**cfg, "reranking_enable": False, "score_threshold_enabled": False}
        lists: list[list[Document]] = []
        for n, (text, method) in enumerate(variants):
            c = {**inner, "search_method": method or cfg["search_method"]}
            docs, _ = await self._candidates(
                ds, text, c, rec, conditions=conditions, fuse=True, variant=n, **labels
            )
            lists.append(docs)
        with rec.stage(
            "merged", dataset_uuid=dsu, method="rrf", variants=len(lists), **labels
        ) as st:
            docs = rrf(lists, key=lambda d: d.metadata["point_id"])
            st.set(docs)
        if cfg["reranking_enable"] and docs:
            qvec = None
            if cfg["reranking_mode"] == "weighted_score":
                qvec = await self._embedder(ds).embed_query(query)
            docs = await self._rerank(cfg, query, docs, qvec, rec, dataset_uuid=dsu, **labels)
            docs = self._threshold(cfg, docs, rec, dataset_uuid=dsu, **labels)
        return docs

    def _mmr(
        self, cfg: dict[str, Any], docs: list[Document], rec: RetrievalRecorder, **labels: Any
    ) -> list[Document]:
        """MMR（§7.3）：按「相关性 − 与已选结果的最大相似度」挑，降低近似重复。
        没有向量的候选只看相关性。"""
        if not cfg["mmr_enable"] or len(docs) < 3:
            return docs
        lam = cfg["mmr_lambda"]
        with rec.stage("diversity", method="mmr", mmr_lambda=lam, **labels) as st:
            top = max(float(d.metadata["score"]) for d in docs) or 1.0
            pool = list(docs)
            picked: list[Document] = []
            while pool:
                best, best_v = None, -1e9
                for d in pool:
                    rel = float(d.metadata["score"]) / top
                    sim = max(
                        (
                            cosine(d.vector, p.vector)
                            for p in picked
                            if d.vector is not None and p.vector is not None
                        ),
                        default=0.0,
                    )
                    v = lam * rel - (1 - lam) * sim
                    if v > best_v:
                        best, best_v = d, v
                assert best is not None
                picked.append(best)
                pool.remove(best)
            st.set(picked)
        return picked

    @staticmethod
    def _per_document(
        cfg: dict[str, Any], result: list[RetrievedSegment], rec: RetrievalRecorder, **labels: Any
    ) -> list[RetrievedSegment]:
        cap = cfg["max_per_document"]
        if not cap:
            return result
        with rec.stage("diversity", method="per_document", max_per_document=cap, **labels) as st:
            count: dict[str, int] = {}
            out = []
            for r in result:
                n = count.get(r.document_uuid, 0)
                if n < cap:
                    out.append(r)
                    count[r.document_uuid] = n + 1
            st.set(out)
        return out

    @staticmethod
    def _merge_adjacent(
        cfg: dict[str, Any], result: list[RetrievedSegment], rec: RetrievalRecorder, **labels: Any
    ) -> list[RetrievedSegment]:
        """同一文档里位置相邻、都命中的分段合并（并到分数高的那个上，正文按位置拼接）。"""
        if not cfg["merge_adjacent"] or len(result) < 2:
            return result
        with rec.stage("diversity", method="merge_adjacent", **labels) as st:
            groups: dict[str, list[RetrievedSegment]] = {}
            for r in result:
                groups.setdefault(r.document_uuid, []).append(r)
            absorbed: set[str] = set()
            for items in groups.values():
                items = sorted(items, key=lambda r: r.position)
                run: list[RetrievedSegment] = []
                for r in [*items, None]:
                    if r is not None and run and r.position == run[-1].position + 1:
                        run.append(r)
                        continue
                    if len(run) > 1:
                        head = max(run, key=lambda x: x.score)
                        head.content = "\n\n".join(x.content for x in run)
                        head.merged_segments = [x.segment_uuid for x in run if x is not head]
                        head.child_chunks = [c for x in run for c in x.child_chunks]
                        head.position, head.position_end = run[0].position, run[-1].position
                        absorbed.update(head.merged_segments)
                    run = [r] if r is not None else []
            out = [r for r in result if r.segment_uuid not in absorbed]
            st.set(out)
        return out

    async def _finish(
        self,
        datasets: dict[str, Dataset],
        docs: list[Document],
        cfg: dict[str, Any],
        rec: RetrievalRecorder,
        *,
        record_final: bool = True,
        **labels: Any,
    ) -> list[RetrievedSegment]:
        top_k = cfg["top_k"]
        docs = self._mmr(cfg, docs, rec, **labels)
        with rec.stage("assemble", **labels) as st:
            result = await self._assemble(datasets, docs)
            st.set(result)
        result = self._per_document(cfg, result, rec, **labels)
        result = self._merge_adjacent(cfg, result, rec, **labels)
        if not record_final:
            return result[:top_k]
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
        *,
        plan: QueryPlan | None = None,
        conditions: Any = None,
    ) -> list[RetrievedSegment]:
        rec = rec or RetrievalRecorder()
        cfg = normalize_retrieval_config(cfg if cfg is not None else ds.retrieval_config)
        plan = plan or QueryPlan.plain(query)
        if plan.sub_questions:
            return await self._decomposed([ds], plan, cfg, rec, {str(ds.uuid): conditions})
        docs = await self._recall(ds, plan.variants, plan.query, cfg, rec, conditions=conditions)
        return await self._finish({str(ds.uuid): ds}, docs, cfg, rec)

    # ------------------------------------------------------------------ 多库

    async def multiple_retrieve(
        self,
        datasets: list[Dataset],
        query: str,
        cfg: dict[str, Any],
        rec: RetrievalRecorder | None = None,
        *,
        plan: QueryPlan | None = None,
        conditions: dict[str, Any] | None = None,
    ) -> list[RetrievedSegment]:
        """各库按自己的检索方式召回，再按 cfg（top_k / 阈值 / 重排）统一收口。

        conditions：库 uuid → 该库的附加过滤（元数据字段各库不同）。
        """
        rec = rec or RetrievalRecorder()
        cfg = normalize_retrieval_config(cfg)
        plan = plan or QueryPlan.plain(query)
        conditions = conditions or {}
        if not datasets:
            return []
        if plan.sub_questions:
            return await self._decomposed(datasets, plan, cfg, rec, conditions)
        if len(datasets) == 1:
            ds = datasets[0]
            own = {**normalize_retrieval_config(ds.retrieval_config), **_overrides(cfg)}
            docs = await self._recall(
                ds, plan.variants, plan.query, own, rec, conditions=conditions.get(str(ds.uuid))
            )
            return await self._finish({str(ds.uuid): ds}, docs, own, rec)
        merged = await self._multi_recall(datasets, plan, cfg, rec, conditions)
        return await self._finish({str(d.uuid): d for d in datasets}, merged, cfg, rec)

    async def _multi_recall(
        self,
        datasets: list[Dataset],
        plan: QueryPlan,
        cfg: dict[str, Any],
        rec: RetrievalRecorder,
        conditions: dict[str, Any],
        **labels: Any,
    ) -> list[Document]:
        per_ds: list[list[Document]] = []
        for ds in datasets:
            # ★ 不并发：同一个 AsyncSession 不能并发执行查询。库数一般个位数，串行可接受
            own = normalize_retrieval_config(ds.retrieval_config)
            own["top_k"] = cfg["top_k"]
            docs = await self._recall(
                ds,
                plan.variants,
                plan.query,
                own,
                rec,
                conditions=conditions.get(str(ds.uuid)),
                **labels,
            )
            per_ds.append(docs)
        multi_variant = len(plan.variants) > 1
        with rec.stage("merged", method="rrf" if multi_variant else None, **labels) as st:
            if multi_variant and not cfg["reranking_enable"]:
                # 各库的分数来自不同表述的 RRF，量纲不一：跨库也按名次融合
                merged = rrf(per_ds, key=lambda d: d.metadata["point_id"])
            else:
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
                    qvec = await self._embedder(datasets[0]).embed_query(plan.query)
            merged = await self._rerank(cfg, plan.query, merged, qvec, rec, **labels)
            merged = self._threshold(cfg, merged, rec, **labels)
        return merged

    async def _decomposed(
        self,
        datasets: list[Dataset],
        plan: QueryPlan,
        cfg: dict[str, Any],
        rec: RetrievalRecorder,
        conditions: dict[str, Any],
    ) -> list[RetrievedSegment]:
        """复合问题：每个子问题各走一遍（召回 → 重排 → 组装），结果按子问题分组，再 RRF 合并。"""
        by_uuid = {str(d.uuid): d for d in datasets}
        groups: list[list[RetrievedSegment]] = []
        for n, sub in enumerate(plan.sub_questions):
            sub_plan = QueryPlan.plain(sub)
            if len(datasets) == 1:
                ds = datasets[0]
                own = {**normalize_retrieval_config(ds.retrieval_config), **_overrides(cfg)}
                docs = await self._recall(
                    ds,
                    sub_plan.variants,
                    sub,
                    own,
                    rec,
                    conditions=conditions.get(str(ds.uuid)),
                    branch=n,
                )
            else:
                own = cfg
                docs = await self._multi_recall(datasets, sub_plan, cfg, rec, conditions, branch=n)
            result = await self._finish(by_uuid, docs, own, rec, record_final=False, branch=n)
            groups.append(result)
        # 一个分段可能被几个子问题都命中：归到它排名最靠前的那个子问题
        best: dict[str, tuple[int, int]] = {}
        for n, result in enumerate(groups):
            for rank, r in enumerate(result):
                if r.segment_uuid not in best or rank < best[r.segment_uuid][0]:
                    best[r.segment_uuid] = (rank, n)
        for result in groups:
            for r in result:
                r.sub_question = best[r.segment_uuid][1]
        with rec.stage("merged", method="rrf", sub_questions=len(groups)) as st:
            # 每个子问题的头名都先进来（RRF 同名次同分），保证每个子问题都有依据
            merged = rrf(groups, key=lambda r: r.segment_uuid)
            st.set(merged)
        with rec.stage("final", top_k=cfg["top_k"]) as st:
            result = merged[: cfg["top_k"]]
            st.set(result)
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


def rrf[T](lists: list[list[T]], *, key: Any, k: int = _RRF_K) -> list[T]:
    """倒数排名融合：score = Σ 1 / (k + 名次)。同一项取第一次出现的对象，分数写回它上面。"""
    scores: dict[Any, float] = {}
    first: dict[Any, T] = {}
    for items in lists:
        for rank, item in enumerate(items, 1):
            kk = key(item)
            scores[kk] = scores.get(kk, 0.0) + 1.0 / (k + rank)
            first.setdefault(kk, item)
    out = sorted(first, key=lambda kk: scores[kk], reverse=True)
    for kk in out:
        item = first[kk]
        if isinstance(item, Document):
            item.metadata["score"] = scores[kk]
        elif isinstance(item, RetrievedSegment):
            item.score = scores[kk]
    return [first[kk] for kk in out]
