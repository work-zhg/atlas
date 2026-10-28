"""召回测试与多库检索的入口：权限 + 检索 + 记录（同 Dify HitTestingService / DatasetRetrieval）。

每次检索：
    span  knowledge.retrieve（挂在调用方的链路下）→ 各环节子 span（rag/recorder.py）
    记录  kb_retrieval 一条（各环节候选，前端画漏斗）+ kb_query_log 每库一条（指回它）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..db.models import Dataset, IndexVersion, QueryLog, Retrieval, Segment
from ..db.models import Document as DocRow
from ..errors import Invalid, NotFound
from ..rag.filters import FieldDef, Scope, scope_filter, validate_filter
from ..rag.query import QueryPlan, understand
from ..rag.recorder import RetrievalRecorder
from ..rag.retrieval import RetrievalService, RetrievedSegment, normalize_retrieval_config
from ..runtime import Runtime
from ..telemetry import SPAN_KIND, current_trace_id, set_content, tracer
from .metadata import fields_of
from .permission import DatasetAccess, load_dataset, readable_ids

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


class _VersionView:
    """知识库换成指定索引版本的模型与 collection（只读视图，不改库里的知识库）。"""

    def __init__(self, ds: Dataset, version: IndexVersion) -> None:
        self._ds = ds
        self.embedding_provider = version.embedding_provider
        self.embedding_model = version.embedding_model
        self.embedding_dim = version.embedding_dim
        self.collection_name = version.collection_name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ds, name)


@dataclass
class RetrieveResult:
    records: list[RetrievedSegment]
    retrieval_id: str
    trace_id: str | None
    elapsed_ms: int
    #: 查询理解的结果（改写、多查询、子问题、抽取的过滤条件、路由）
    plan: dict[str, Any] | None = None
    sub_questions: list[str] | None = None
    #: 内部调用里权限主体读不到（或已删除）、本次跳过的库 id。不区分两种原因：不暴露库是否存在
    skipped: list[str] = field(default_factory=list)

    def as_dict(self, query: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "query": query,
            "records": [r.as_dict() for r in self.records],
            "elapsed_ms": self.elapsed_ms,
            "retrieval_id": self.retrieval_id,
            "trace_id": self.trace_id,
            "skipped": self.skipped,
        }
        if self.plan:
            out["query_plan"] = self.plan
        if self.sub_questions:
            out["sub_questions"] = self.sub_questions
        return out


@dataclass
class RetrieveOptions:
    """检索请求里的企业级参数（§7）。"""

    #: 对话历史 [{role, content}]：问题含指代时用于改写
    history: list[dict[str, str]] | None = None
    #: 元数据过滤表达式（rag/filters.py）
    filter: dict[str, Any] | None = None
    #: 按时间点检索（ISO 8601）
    as_of: datetime | None = None
    include_superseded: bool = False


class KnowledgeRetrieval:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt
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
        principals: dict[str, list[str] | None],
        opts: RetrieveOptions | None = None,
    ) -> RetrieveResult:
        """开根 span → 查询理解 → 检索 → 落 kb_retrieval 与 kb_query_log。

        principals：库 uuid → 主体标签（None = 该库不做文档级权限过滤，库管理员调试用）。
        """
        opts = opts or RetrieveOptions()
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
                eff = normalize_retrieval_config(
                    cfg if cfg is not None else datasets[0].retrieval_config
                )
            else:
                eff = normalize_retrieval_config(cfg or {})
            fields = await fields_of(self._s, [d.uuid for d in datasets])
            union: dict[str, FieldDef] = {}
            for fs in fields.values():
                union.update(fs)
            user_filter = validate_filter(opts.filter, union)

            all_datasets = datasets

            async def run(plan: QueryPlan) -> list[RetrievedSegment]:
                nonlocal datasets
                datasets = all_datasets
                if plan.datasets:
                    datasets = [d for d in datasets if str(d.uuid) in plan.datasets] or datasets
                conditions = {
                    str(d.uuid): scope_filter(
                        Scope(
                            principals=principals.get(str(d.uuid)),
                            as_of=opts.as_of,
                            include_superseded=opts.include_superseded,
                            filter=user_filter,
                            extracted=plan.extracted,
                        ),
                        fields[str(d.uuid)],
                    )
                    for d in datasets
                }
                if single:
                    return await self._svc.retrieve(
                        datasets[0],
                        query,
                        eff,
                        rec,
                        plan=plan,
                        conditions=conditions[str(datasets[0].uuid)],
                    )
                return await self._svc.multiple_retrieve(
                    datasets, query, eff, rec, plan=plan, conditions=conditions
                )

            plan = await self._understand(query, eff, datasets, union, opts, rec, single=single)
            records = await run(plan)
            # 自适应多查询（§7.1）：首轮最高分低于阈值、且这轮没用多个表述 → 生成多个表述再检索一次
            threshold = eff["adaptive_multi_query_threshold"]
            top = records[0].score if records else 0.0
            if (
                threshold
                and top < threshold
                and (self._rt.query_llm or self._rt.llm) is not None
                and len(plan.variants) == 1
                and not plan.sub_questions
            ):
                mark = len(rec.stages)
                retry = await self._understand(
                    query,
                    {**eff, "multi_query": True},
                    all_datasets,
                    union,
                    opts,
                    rec,
                    single=single,
                )
                if len(retry.variants) > 1:
                    # 漏斗只留第二轮（首轮的环节去掉，避免同一分段在两轮里「掉了又回来」）
                    first = [s for s in rec.stages[:mark] if s.name == "query_understanding"]
                    rec.stages = [*first, *rec.stages[mark:]]
                    retry.notes = {**plan.notes, **retry.notes, "adaptive": round(top, 4)}
                    plan = retry
                    records = await run(plan)
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
                config={
                    **eff,
                    "_scope": {
                        "filter": user_filter,
                        "as_of": opts.as_of.isoformat() if opts.as_of else None,
                        "include_superseded": opts.include_superseded,
                        "acl": {
                            u: ("bypass" if t is None else "enforced")
                            for u, t in principals.items()
                        },
                    },
                },
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
        return RetrieveResult(
            records,
            str(rid),
            trace_id,
            elapsed,
            plan=plan.notes or None,
            sub_questions=plan.sub_questions or None,
        )

    async def _understand(
        self,
        query: str,
        cfg: dict[str, Any],
        datasets: list[Any],
        fields: dict[str, FieldDef],
        opts: RetrieveOptions,
        rec: RetrievalRecorder,
        *,
        single: bool,
    ) -> QueryPlan:
        """阶段 ⓪：查询理解。不需要（或没配 LLM）时直接返回原问题，不进漏斗。"""
        with rec.stage("query_understanding", span_kind="CHAIN") as st:
            plan = await understand(
                query,
                cfg=cfg,
                llm=self._rt.query_llm or self._rt.llm,
                redis=self._rt.redis,
                history=opts.history,
                fields={k: f for k, f in fields.items() if f.llm_extract},
                datasets=None
                if single
                else [
                    {"id": str(d.uuid), "name": d.name, "description": d.description}
                    for d in datasets
                ],
            )
            if plan.notes:
                st.set([], **plan.notes)
        return plan

    async def hit_testing(
        self,
        access: DatasetAccess,
        query: str,
        retrieval_config: dict[str, Any] | None,
        *,
        version: IndexVersion | None = None,
        opts: RetrieveOptions | None = None,
    ) -> dict[str, Any]:
        query = _query(query)
        ds: Any = access.ds
        if version is not None and version.collection_name != ds.collection_name:
            if version.status not in ("ready", "retired", "building"):
                raise Invalid(f"版本状态为 {version.status}，不能用于检索")
            ds = _VersionView(access.ds, version)
        res = await self._traced(
            [ds],
            query,
            retrieval_config,
            source="hit_testing",
            source_ref=None,
            actor=access.p.uid,
            single=True,
            principals={str(ds.uuid): await self._acl(access)},
            opts=opts,
        )
        return res.as_dict(query)

    async def _acl(self, access: DatasetAccess) -> list[str] | None:
        """库管理员不受文档级权限限制（调试、管理）；其他人按主体标签过滤。"""
        if access.is_admin:
            return None
        return await self._rt.principals(access.p.auth_uid)

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
        acl_subject: str | None = None,
        opts: RetrieveOptions | None = None,
    ) -> RetrieveResult:
        """p 为 None = 内部调用（智能体运行时、会话 Pod 里的 MCP）。

        库级、文档级权限都按这一轮的 auth-uid（acl_subject）判：以 Agent 为权限时是它的数字员工，
        以人为权限时是使用者本人（内网身份传递 §8）。读不到的库跳过、记进 skipped，由调用方提示；
        没有主体一个库都读不到。actor 只进检索记录（谁在用）。
        """
        query = _query(query)
        if not dataset_uuids:
            raise Invalid("请选择知识库")
        datasets: list[Dataset] = []
        principals: dict[str, list[str] | None] = {}
        skipped: list[str] = []
        if p is not None:
            for u in dict.fromkeys(dataset_uuids):
                access = await load_dataset(self._s, self._rt.perms, u, p)
                datasets.append(access.ds)
                principals[str(u)] = await self._acl(access)
        else:
            wanted = list(dict.fromkeys(dataset_uuids))
            rows = {
                d.uuid: d
                for d in (
                    await self._s.scalars(select(Dataset).where(Dataset.uuid.in_(wanted)))
                ).all()
            }
            subject = acl_subject or actor
            ok = await readable_ids(self._rt.perms, subject, [u for u in wanted if u in rows])
            datasets = [rows[u] for u in wanted if u in ok]
            skipped = [str(u) for u in wanted if u not in ok]
            if not datasets or subject is None:
                return RetrieveResult(
                    records=[], retrieval_id="", trace_id=None, elapsed_ms=0, skipped=skipped
                )
            tags = await self._rt.principals(subject)
            principals = {str(d.uuid): tags for d in datasets}
        res = await self._traced(
            datasets,
            query,
            retrieval_config,
            source=source,
            source_ref=source_ref,
            actor=actor or (p.uid if p else "system"),
            single=False,
            principals=principals,
            opts=opts,
        )
        res.skipped = skipped
        return res

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
                await load_dataset(self._s, self._rt.perms, UUID(u), p)
                readable += 1
            except NotFound:
                exists = await self._s.scalar(select(Dataset.id).where(Dataset.uuid == UUID(u)))
                if exists is not None:  # 库还在但读不了
                    raise
                # 库已删：剩下的库能读就给看
        if not readable and row.created_by != p.uid:
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
            # 这个分段走过的环节：本库的环节 + 跨库环节；并行的召回（两路、多个表述）不算「掉」；
            # 子问题（branch）的环节只算它出现过的那几个分支
            branches = {
                (st.get("attrs") or {}).get("branch")
                for i, st in enumerate(stages)
                if key in cells[i]
            }
            path = [
                i
                for i, st in enumerate(stages)
                if st.get("dataset_uuid") in (None, ds_uuid)
                and (st.get("attrs") or {}).get("branch") in (None, *branches)
            ]
            seen = False
            dropped_at: int | None = None
            for i in path:
                present = key in cells[i]
                parallel = stages[i]["name"] in _PARALLEL or "variant" in (
                    stages[i].get("attrs") or {}
                )
                if present:
                    seen = True
                elif seen and not parallel:
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
