"""把分段写进向量库（入库第 ⑥ 步；分段编辑后重建也走这里）。

    text_model          每个分段一个 point（id = 分段 uuid）
    hierarchical_model  每个子块一个 point（id = 子块 uuid），payload.segment_uuid 指回父分段

★ point id 用库里的 uuid：重跑是覆盖不是追加，入库可以安全重试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildChunk, Dataset, Document, Segment
from ..rag.embedding import CacheEmbedding
from ..rag.keyword import bm25_document
from ..rag.models import text_hash
from ..rag.vdb import VectorPoint
from ..runtime import Runtime

__all__ = ["index_segments", "point_ids_of", "remove_segments"]


def _embedder(rt: Runtime, ds: Dataset) -> CacheEmbedding:
    model = rt.registry.embedding(ds.embedding_provider, ds.embedding_model)
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
) -> int:
    """向量化并写入；返回写入的 point 数。调用方负责提交事务。"""
    if not segments:
        return 0
    units: list[tuple[str, str, Segment, ChildChunk | None]] = []
    if doc.doc_form == "hierarchical_model":
        children = await _children(session, segments)
        for seg in segments:
            for child in children.get(str(seg.uuid), []):
                units.append((str(child.uuid), child.content, seg, child))
    else:
        units = [(str(seg.uuid), seg.content, seg, None) for seg in segments]

    store = rt.vectors.for_collection(ds.collection_name)
    await store.create(ds.embedding_dim)
    texts = [u[1] for u in units]
    if texts:
        dense = await _embedder(rt, ds).embed_documents(
            session, texts, batch=rt.settings.embedding_batch
        )
        sparse = await asyncio.to_thread(lambda: [bm25_document(t) for t in texts])
        await store.add(
            [
                VectorPoint(
                    id=pid,
                    dense=vec,
                    sparse_indices=sp[0],
                    sparse_values=sp[1],
                    payload={
                        "kind": "child" if child is not None else "segment",
                        "segment_uuid": str(seg.uuid),
                        "document_uuid": str(doc.uuid),
                        "dataset_uuid": str(ds.uuid),
                        "enabled": seg.enabled,
                        "doc_ok": doc.available,
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
            seg.index_node_hash = text_hash(seg.content)
        seg.status = "completed"
        seg.error = None
    return len(units)


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
    await rt.vectors.for_collection(ds.collection_name).delete_by_ids(ids)
    seg_ids = [s.uuid for s in segments]
    for c in (
        await session.scalars(select(ChildChunk).where(ChildChunk.segment_uuid.in_(seg_ids)))
    ).all():
        await session.delete(c)
    for s in segments:
        await session.delete(s)
