"""召回测试与多库检索的入口：权限 + 检索 + 记录（同 Dify HitTestingService / DatasetRetrieval）。

每次检索：
    span  knowledge.retrieve（挂在调用方的链路下）→ 各环节子 span（rag/recorder.py）
    记录  kb_retrieval 一条（各环节候选，前端画漏斗）+ kb_query_log 每库一条（指回它）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset, QueryLog, Retrieval, Segment
from ..db.models import Document as DocRow
from ..errors import Invalid, NotFound
from ..rag.recorder import RetrievalRecorder
from ..rag.retrieval import RetrievalService, RetrievedSegment, normalize_retrieval_config
from ..runtime import Runtime
from ..telemetry import SPAN_KIND, current_trace_id, set_content, tracer
from .permission import DatasetAccess, load_dataset

__all__ = ["KnowledgeRetrieval", "RetrieveResult"]

_THREAD = re.compile(r"thread:([0-9a-fA-F-]{36})")

_MAX_QUERY = 2000


def _query(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise Invalid("请输入查询内容")
    if len(text) > _MAX_QUERY:
        raise Invalid(f"查询内容不能超过 {_MAX_QUERY} 字")
    return text


@dataclass
class RetrieveResult:
    records: list[RetrievedSegment]
    retrieval_id: str
    trace_id: str | None
    elapsed_ms: int


class KnowledgeRetrieval:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._settings = rt.settings
        self._svc = RetrievalService(
            session,
            registry=rt.registry,
            vectors=rt.vectors,
            redis=rt.redis,
            query_ttl_s=rt.settings.query_embedding_ttl_s,
        )

    async def _traced(
        self,
        datasets: list[Dataset],
        query: str,
        cfg: dict[str, Any] | None,
        *,
        source: str,
        source_ref: str | None,
        actor: str,
        single: bool,
    ) -> RetrieveResult:
        """开根 span → 检索 → 落 kb_retrieval 与 kb_query_log。"""
        rec = RetrievalRecorder()
        rid = uuid4()
        with tracer().start_as_current_span("knowledge.retrieve") as span:
            span.set_attribute(SPAN_KIND, "RETRIEVER")
            span.set_attribute("knowledge.retrieval_id", str(rid))
            span.set_attribute("knowledge.source", source)
            span.set_attribute("knowledge.dataset_uuids", [str(d.uuid) for d in datasets])
            if source_ref:
                span.set_attribute("knowledge.source_ref", source_ref)
                if m := _THREAD.search(source_ref):
                    span.set_attribute("gen_ai.conversation.id", m.group(1))
                    span.set_attribute("langfuse.session.id", m.group(1))
            if actor:
                span.set_attribute("langfuse.user.id", actor)
            set_content(span, input=query)
            started = time.monotonic()
            if single:
                ds = datasets[0]
                eff = normalize_retrieval_config(cfg if cfg is not None else ds.retrieval_config)
                records = await self._svc.retrieve(ds, query, eff, rec)
            else:
                eff = normalize_retrieval_config(cfg or {})
                records = await self._svc.multiple_retrieve(datasets, query, eff, rec)
            elapsed = int((time.monotonic() - started) * 1000)
            span.set_attribute("knowledge.search_method", eff["search_method"])
            span.set_attribute("knowledge.top_k", eff["top_k"])
            span.set_attribute("knowledge.results", len(records))
            set_content(
                span,
                output=[
                    {
                        "segment": r.segment_uuid,
                        "document": r.document_name,
                        "score": r.score,
                        "title_path": r.title_path,
                        "content": r.content,
                    }
                    for r in records
                ],
            )
            trace_id = current_trace_id()

        self._s.add(
            Retrieval(
                uuid=rid,
                dataset_uuids=[str(d.uuid) for d in datasets],
                query=query,
                source=source,
                source_ref=source_ref,
                created_by=actor,
                config=eff,
                stages=rec.dump(),
                results=[{"segment": r.segment_uuid, "score": round(r.score, 6)} for r in records],
                trace_id=trace_id,
                latency_ms=elapsed,
            )
        )
        for ds in datasets:
            n = sum(1 for h in records if h.dataset_uuid == str(ds.uuid))
            self._s.add(
                QueryLog(
                    dataset_uuid=ds.uuid,
                    query=query[:_MAX_QUERY],
                    source=source,
                    source_ref=source_ref,
                    hits=n,
                    created_by=actor,
                    retrieval_uuid=rid,
                )
            )
        return RetrieveResult(records, str(rid), trace_id, elapsed)

    async def hit_testing(
        self, access: DatasetAccess, query: str, retrieval_config: dict[str, Any] | None
    ) -> dict[str, Any]:
        query = _query(query)
        res = await self._traced(
            [access.ds],
            query,
            retrieval_config,
            source="hit_testing",
            source_ref=None,
            actor=access.p.id,
            single=True,
        )
        return {
            "query": query,
            "records": [r.as_dict() for r in res.records],
            "elapsed_ms": res.elapsed_ms,
            "retrieval_id": res.retrieval_id,
            "trace_id": res.trace_id,
        }

    async def retrieve(
        self,
        p: Principal | None,
        dataset_uuids: list[UUID],
        query: str,
        retrieval_config: dict[str, Any] | None,
        *,
        source: str,
        source_ref: str | None = None,
        actor: str | None = None,
    ) -> RetrieveResult:
        """p 为 None = 内部调用（atlas-server 已按智能体绑定判过权限）。"""
        query = _query(query)
        if not dataset_uuids:
            raise Invalid("请选择知识库")
        datasets: list[Dataset] = []
        if p is not None:
            for u in dict.fromkeys(dataset_uuids):
                datasets.append((await load_dataset(self._s, u, p)).ds)
        else:
            rows = (
                await self._s.scalars(
                    select(Dataset).where(Dataset.uuid.in_(list(set(dataset_uuids))))
                )
            ).all()
            if not rows:
                raise NotFound("知识库不存在")
            datasets = list(rows)
        return await self._traced(
            datasets,
            query,
            retrieval_config,
            source=source,
            source_ref=source_ref,
            actor=actor or (p.id if p else "system"),
            single=False,
        )

    async def queries(self, access: DatasetAccess, *, page: int, limit: int) -> list[QueryLog]:
        rows = await self._s.scalars(
            select(QueryLog)
            .where(QueryLog.dataset_uuid == access.ds.uuid)
            .order_by(QueryLog.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        return list(rows.all())

    async def get(self, p: Principal, retrieval_id: UUID) -> Retrieval:
        """检索记录：须能读其中每个库（跨库检索的记录，少一个库的权限也不给看）。"""
        row = await self._s.scalar(select(Retrieval).where(Retrieval.uuid == retrieval_id))
        if row is None:
            raise NotFound("检索记录不存在")
        readable = 0
        for u in row.dataset_uuids:
            try:
                await load_dataset(self._s, UUID(u), p)
                readable += 1
            except NotFound:
                exists = await self._s.scalar(select(Dataset.id).where(Dataset.uuid == UUID(u)))
                if exists is not None:  # 库还在但读不了
                    raise
                # 库已删：剩下的库能读就给看
        if not readable and row.created_by != p.id:
            raise NotFound("检索记录不存在")
        return row

    async def funnel(self, row: Retrieval) -> dict[str, Any]:
        """分段 × 环节的矩阵：每个候选分段在各环节的名次与分数，以及它在哪一环掉的。"""
        stages = row.stages or []
        seg_ids: dict[str, None] = {}
        for st in stages:
            for it in st["items"]:
                if it.get("segment"):
                    seg_ids.setdefault(it["segment"])
        uuids = [UUID(s) for s in list(seg_ids)[:_FUNNEL_MAX]]
        segs = {
            str(s.uuid): s
            for s in (await self._s.scalars(select(Segment).where(Segment.uuid.in_(uuids)))).all()
        }
        doc_names = dict(
            (
                await self._s.execute(
                    select(DocRow.uuid, DocRow.name).where(
                        DocRow.uuid.in_({s.document_uuid for s in segs.values()})
                    )
                )
            ).all()
        )
        ds_names = dict(
            (
                await self._s.execute(
                    select(Dataset.uuid, Dataset.name).where(
                        Dataset.uuid.in_([UUID(u) for u in row.dataset_uuids])
                    )
                )
            ).all()
        )

        # 每个环节里，分段的最好名次 / 分数 / 命中的 point 数（父子分段时一个分段可能有多个子块）
        cells: list[dict[str, dict[str, Any]]] = []
        for st in stages:
            best: dict[str, dict[str, Any]] = {}
            for rank, it in enumerate(st["items"], 1):
                sid = it.get("segment")
                if not sid:
                    continue
                cur = best.get(sid)
                if cur is None:
                    best[sid] = {"rank": rank, "score": it["score"], "points": 1}
                else:
                    cur["points"] += 1
            cells.append(best)

        rows: list[dict[str, Any]] = []
        for sid in uuids:
            key = str(sid)
            seg = segs.get(key)
            ds_uuid = str(seg.dataset_uuid) if seg else _segment_dataset(stages, key)
            # 这个分段走过的环节：本库的环节 + 跨库环节；并行的两路召回不算「掉」
            path = [i for i, st in enumerate(stages) if st.get("dataset_uuid") in (None, ds_uuid)]
            seen = False
            dropped_at: int | None = None
            for i in path:
                present = key in cells[i]
                if present:
                    seen = True
                elif seen and stages[i]["name"] not in _PARALLEL:
                    dropped_at = i
                    break
            final_rank = next(
                (
                    cells[i][key]["rank"]
                    for i in reversed(path)
                    if stages[i]["name"] == "final" and key in cells[i]
                ),
                None,
            )
            if final_rank is not None:
                status = "selected"
            elif dropped_at is None:
                status = "dropped"
            else:
                status = _DROP_STATUS.get(stages[dropped_at]["name"], "dropped")
            rows.append(
                {
                    "segment_uuid": key,
                    "deleted": seg is None,
                    "dataset_uuid": ds_uuid,
                    "dataset_name": ds_names.get(UUID(ds_uuid)) if ds_uuid else None,
                    "document_uuid": str(seg.document_uuid) if seg else None,
                    "document_name": doc_names.get(seg.document_uuid) if seg else None,
                    "position": seg.position if seg else None,
                    "title_path": list(seg.title_path or []) if seg else [],
                    "content": (seg.content[:_PREVIEW] if seg else ""),
                    "status": status,
                    "dropped_at": dropped_at,
                    "final_rank": final_rank,
                    "cells": [cells[i].get(key) for i in range(len(stages))],
                }
            )

        def _order(r: dict[str, Any]) -> tuple[int, int, float]:
            if r["final_rank"] is not None:
                return (0, r["final_rank"], 0.0)
            reached = r["dropped_at"] if r["dropped_at"] is not None else 0
            first = next((c for c in r["cells"] if c), None)
            return (1, -reached, -(first["score"] if first else 0.0))

        rows.sort(key=_order)
        tpl = self._settings.trace_url_template
        return {
            "id": str(row.uuid),
            "query": row.query,
            "source": row.source,
            "source_ref": row.source_ref,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
            "latency_ms": row.latency_ms,
            "trace_id": row.trace_id,
            "trace_url": tpl.replace("{trace_id}", row.trace_id) if tpl and row.trace_id else None,
            "config": row.config,
            "datasets": [{"id": u, "name": ds_names.get(UUID(u))} for u in row.dataset_uuids],
            "stages": [
                {
                    "name": st["name"],
                    "dataset_uuid": st.get("dataset_uuid"),
                    "dataset_name": (
                        ds_names.get(UUID(st["dataset_uuid"])) if st.get("dataset_uuid") else None
                    ),
                    "elapsed_ms": st.get("elapsed_ms"),
                    "attrs": st.get("attrs") or {},
                    "count": len(st["items"]),
                }
                for st in stages
            ],
            "rows": rows,
            "truncated": len(seg_ids) > _FUNNEL_MAX,
        }


#: 并行的召回环节：一个分段只在其中一路出现是正常的，不算「掉」
_PARALLEL = {"semantic", "full_text"}
#: 掉在哪个环节 → 状态
_DROP_STATUS = {
    "threshold": "below_threshold",
    "final": "truncated",
    "assemble": "missing",
}
_FUNNEL_MAX = 200
_PREVIEW = 300


def _segment_dataset(stages: list[dict[str, Any]], segment: str) -> str | None:
    for st in stages:
        if st.get("dataset_uuid") and any(it.get("segment") == segment for it in st["items"]):
            return st["dataset_uuid"]
    return None
