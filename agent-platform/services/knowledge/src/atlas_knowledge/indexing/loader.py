"""把分段写进向量库（入库第 ⑥ 步；分段编辑后重建也走这里）。

    text_model          每个分段一个 point（id = 分段 uuid）
    hierarchical_model  每个子块一个 point（id = 子块 uuid），payload.segment_uuid 指回父分段

★ point id 用库里的 uuid：重跑是覆盖不是追加，入库可以安全重试。
★ 写进所有 collection 还在的索引版本（versions.write_targets）：换模型构建期间双写。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildChunk, Dataset, Document, Segment
from ..rag.embedding import CacheEmbedding
from ..rag.keyword import bm25_document
from ..rag.models import text_hash
from ..rag.structure import path_text
from ..rag.vdb import VectorPoint
from ..runtime import Runtime
from .versions import Target, stores, write_targets

__all__ = [
    "CHILD_KINDS",
    "EXTRA_ENTRY_TYPES",
    "doc_payload",
    "embed_text",
    "index_segments",
    "point_ids_of",
    "point_kind",
    "remove_segments",
    "segment_prefix",
]

#: 通用分段里也要单独建 point 的子块类型（额外的检索入口）
EXTRA_ENTRY_TYPES = frozenset({"summary"})
#: 指向子块表的 point 类型（命中后从 kb_child_chunk 取正文）
CHILD_KINDS = frozenset({"child", "summary"})


def embed_text(content: str, prefix: str | None) -> str:
    """建向量（与 BM25）用的文本 = 前缀（标题路径 / 表头）+ 原文。存储的正文不含前缀。"""
    return f"{prefix}\n{content}" if prefix else content


def segment_prefix(seg: Segment) -> str:
    """分段的向量化前缀：新分段存了完整前缀（文档标题 + 标题路径 + LLM 上下文）；
    老分段用标题路径。"""
    return seg.context if seg.context is not None else path_text(seg.title_path)


def point_kind(child: ChildChunk | None) -> str:
    if child is None:
        return "segment"
    return "summary" if child.type == "summary" else "child"


def doc_payload(doc: Document) -> dict[str, Any]:
    """文档级 payload（§6.3）：可用、元数据、权限、版本与时效。变更时按文档整体覆盖。

    时间存 epoch 秒；没有生效区间的键为 None（写入时不写、更新时删掉），检索按「不限」处理。
    """
    ext = (doc.data_source_info or {}).get("extension") or doc.name.rsplit(".", 1)[-1]
    builtin = {
        "doc_type": str(ext).lower().lstrip("."),
        "author": doc.created_by,
        "uploaded_at": doc.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if doc.created_at
        else None,
    }
    return {
        "doc_ok": doc.available,
        "meta": {**{k: v for k, v in builtin.items() if v}, **(doc.doc_metadata or {})},
        "acl": list(doc.acl) if doc.acl else ["public"],
        "is_current": bool(doc.is_current),
        "valid_from": doc.valid_from.timestamp() if doc.valid_from else None,
        "valid_to": doc.valid_to.timestamp() if doc.valid_to else None,
    }


def segment_meta(shared: dict[str, Any], seg: Segment) -> dict[str, Any]:
    """point 的 meta = 文档元数据 + 分段覆盖（同名以分段为准）。"""
    return {**shared["meta"], **(seg.meta or {})}


async def sync_doc_payload(session: AsyncSession, rt: Runtime, ds: Dataset, doc: Document) -> None:
    """文档级字段变了：按文档整体覆盖，再把有分段级覆盖的分段补写回去。"""
    shared = doc_payload(doc)
    overridden = (
        await session.scalars(
            select(Segment).where(Segment.document_uuid == doc.uuid, Segment.meta.is_not(None))
        )
    ).all()
    targets = await stores(session, rt, ds)
    for store in targets:
        await store.set_doc_payload(str(doc.uuid), shared)
    for seg in overridden:
        await sync_segment_meta(session, rt, ds, doc, seg, shared=shared, targets=targets)


async def sync_segment_meta(
    session: AsyncSession,
    rt: Runtime,
    ds: Dataset,
    doc: Document,
    seg: Segment,
    *,
    shared: dict[str, Any] | None = None,
    targets: list[Any] | None = None,
) -> None:
    ids = await point_ids_of(session, [seg])
    meta = segment_meta(shared or doc_payload(doc), seg)
    for store in targets if targets is not None else await stores(session, rt, ds):
        await store.set_points_payload(ids, {"meta": meta})


async def _embedder(rt: Runtime, target: Target) -> CacheEmbedding:
    model = await asyncio.to_thread(rt.registry.embedding, target.provider, target.model)
    if model.dim != target.dim:
        raise RuntimeError(
            f"向量模型 {target.provider}/{target.model} 的维度是 {model.dim}，"
            f"索引记录的是 {target.dim}"
        )
    return CacheEmbedding(model, redis=rt.redis, query_ttl_s=rt.settings.query_embedding_ttl_s)


async def _children(
    session: AsyncSession, segments: Sequence[Segment]
) -> dict[str, list[ChildChunk]]:
    if not segments:
        return {}
    rows = await session.scalars(
        select(ChildChunk)
        .where(ChildChunk.segment_uuid.in_([s.uuid for s in segments]))
        .order_by(ChildChunk.position)
    )
    out: dict[str, list[ChildChunk]] = {}
    for c in rows.all():
        out.setdefault(str(c.segment_uuid), []).append(c)
    return out


async def index_segments(
    session: AsyncSession,
    rt: Runtime,
    ds: Dataset,
    doc: Document,
    segments: Sequence[Segment],
    *,
    targets: list[Target] | None = None,
) -> int:
    """向量化并写入；返回写入的 point 数（每个目标版本各算一次）。调用方负责提交事务。

    targets：只写这些索引版本（重建新版本时）；默认写所有 collection 还在的版本。
    """
    if not segments:
        return 0
    units: list[tuple[str, str, Segment, ChildChunk | None]] = []
    children = await _children(session, segments)
    if doc.doc_form == "hierarchical_model":
        for seg in segments:
            for child in children.get(str(seg.uuid), []):
                units.append(
                    (str(child.uuid), embed_text(child.content, child.context), seg, child)
                )
    else:
        units = [
            (str(seg.uuid), embed_text(seg.content, segment_prefix(seg)), seg, None)
            for seg in segments
        ]
        # 通用分段也可以有额外的检索入口（大表摘要，§5.5）：各自一个 point，命中回到分段
        for seg in segments:
            for child in children.get(str(seg.uuid), []):
                if child.type in EXTRA_ENTRY_TYPES:
                    units.append(
                        (str(child.uuid), embed_text(child.content, child.context), seg, child)
                    )

    texts = [u[1] for u in units]
    shared = doc_payload(doc)
    sparse = await asyncio.to_thread(lambda: [bm25_document(t) for t in texts]) if texts else []
    written = 0
    for target in targets if targets is not None else await write_targets(session, ds):
        store = rt.vectors.for_collection(target.collection)
        await store.create(target.dim)
        if not texts:
            continue
        embedder = await _embedder(rt, target)
        dense = await embedder.embed_documents(session, texts, batch=rt.settings.embedding_batch)
        written += len(texts)
        await store.add(
            [
                VectorPoint(
                    id=pid,
                    dense=vec,
                    sparse_indices=sp[0],
                    sparse_values=sp[1],
                    payload={
                        **{k: v for k, v in shared.items() if v is not None},
                        "meta": segment_meta(shared, seg),
                        "kind": point_kind(child),
                        "segment_uuid": str(seg.uuid),
                        "document_uuid": str(doc.uuid),
                        "dataset_uuid": str(ds.uuid),
                        "enabled": seg.enabled,
                    },
                )
                for (pid, _, seg, child), vec, sp in zip(units, dense, sparse, strict=True)
            ]
        )
    for pid, text, _seg, child in units:
        if child is not None:
            child.index_node_id = pid
            child.index_node_hash = text_hash(text)
    for seg in segments:
        if doc.doc_form != "hierarchical_model":
            seg.index_node_id = str(seg.uuid)
            seg.index_node_hash = text_hash(embed_text(seg.content, segment_prefix(seg)))
        seg.status = "completed"
        seg.error = None
    return written


async def point_ids_of(session: AsyncSession, segments: Sequence[Segment]) -> list[str]:
    ids = [s.index_node_id for s in segments if s.index_node_id]
    children = await _children(session, segments)
    ids += [c.index_node_id for cs in children.values() for c in cs if c.index_node_id]
    return ids


async def remove_segments(
    session: AsyncSession, rt: Runtime, ds: Dataset, segments: Sequence[Segment]
) -> None:
    """删向量（分段与子块），再删库行。"""
    if not segments:
        return
    ids = await point_ids_of(session, segments)
    for store in await stores(session, rt, ds):
        await store.delete_by_ids(ids)
    seg_ids = [s.uuid for s in segments]
    for c in (
        await session.scalars(select(ChildChunk).where(ChildChunk.segment_uuid.in_(seg_ids)))
    ).all():
        await session.delete(c)
    for s in segments:
        await session.delete(s)
