"""索引版本（doc/knowledge-enterprise-design.html §6.2）：双写目标、重建、原子切换。

    write_targets   写向量时的目标：active 版本 + 其余 collection 还在的版本（构建中、待切换、
                    已退役但保留用于回滚）。分段内容只有一份，各版本只是向量不同
    stores          删除 / 开关 / payload 变更要同步到的所有 collection
    rebuild         后台任务：用新模型为所有已完成的文档重新向量化，写进新 collection
    activate        原子切换：一个事务里改 kb_dataset 上的模型与 collection 指针

★ 构建期间新入库的文档：入库写向量时读一次 write_targets，构建中的版本也在里面（双写）。
  入库开始得早、读到 write_targets 时版本还没建的文档，由重建的「补扫」兜住：重建循环到
  没有新完成的文档为止，最后删掉新 collection 里库中已不存在的 point。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import (
    LIVE_INDEX_STATUSES,
    ChildChunk,
    Dataset,
    Document,
    IndexVersion,
    Segment,
)
from ..db.types import utcnow
from ..rag.vdb import BaseVector
from ..runtime import Runtime

__all__ = ["Target", "activate", "rebuild", "stores", "write_targets"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    collection: str
    provider: str
    model: str
    dim: int


def active_target(ds: Dataset) -> Target:
    return Target(ds.collection_name, ds.embedding_provider, ds.embedding_model, ds.embedding_dim)


async def live_versions(session: AsyncSession, ds: Dataset) -> list[IndexVersion]:
    rows = await session.scalars(
        select(IndexVersion)
        .where(
            IndexVersion.dataset_uuid == ds.uuid,
            IndexVersion.status.in_(LIVE_INDEX_STATUSES),
        )
        .order_by(IndexVersion.version)
    )
    return list(rows.all())


async def write_targets(session: AsyncSession, ds: Dataset) -> list[Target]:
    out = [active_target(ds)]
    for v in await live_versions(session, ds):
        if v.collection_name != ds.collection_name:
            out.append(
                Target(v.collection_name, v.embedding_provider, v.embedding_model, v.embedding_dim)
            )
    return out


async def stores(session: AsyncSession, rt: Runtime, ds: Dataset) -> list[BaseVector]:
    return [rt.vectors.for_collection(t.collection) for t in await write_targets(session, ds)]


async def activate(session: AsyncSession, ds: Dataset, version: IndexVersion) -> None:
    """切换 active 指针（调用方提交事务）。旧的 active 版本退役，collection 保留用于回滚。"""
    if version.status not in ("ready", "retired", "active"):
        raise ValueError(f"版本状态为 {version.status}，不能切换")
    for v in await live_versions(session, ds):
        if v.status == "active" and v.uuid != version.uuid:
            v.status = "retired"
    ds.embedding_provider = version.embedding_provider
    ds.embedding_model = version.embedding_model
    ds.embedding_dim = version.embedding_dim
    ds.collection_name = version.collection_name
    version.status = "active"
    version.activated_at = utcnow()


async def _valid_point_ids(session: AsyncSession, ds: Dataset) -> set[str]:
    seg_ids = await session.scalars(
        select(Segment.index_node_id).where(
            Segment.dataset_uuid == ds.uuid, Segment.index_node_id.is_not(None)
        )
    )
    child_ids = await session.scalars(
        select(ChildChunk.index_node_id).where(
            ChildChunk.dataset_uuid == ds.uuid, ChildChunk.index_node_id.is_not(None)
        )
    )
    return {str(x) for x in seg_ids.all()} | {str(x) for x in child_ids.all()}


async def rebuild(rt: Runtime, sm: async_sessionmaker[AsyncSession], version_uuid: str) -> None:
    from .loader import index_segments

    vid = UUID(version_uuid)
    async with sm() as s:
        version = await s.scalar(select(IndexVersion).where(IndexVersion.uuid == vid))
        if version is None or version.status != "building":
            return
        ds_uuid = version.dataset_uuid
        target = Target(
            version.collection_name,
            version.embedding_provider,
            version.embedding_model,
            version.embedding_dim,
        )
    store = rt.vectors.for_collection(target.collection)
    await store.create(target.dim)
    done: set[UUID] = set()
    points = 0
    try:
        while True:
            async with sm() as s:
                ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
                if ds is None:
                    return
                todo = list(
                    (
                        await s.scalars(
                            select(Document.uuid).where(
                                Document.dataset_uuid == ds_uuid,
                                Document.indexing_status == "completed",
                            )
                        )
                    ).all()
                )
            todo = [d for d in todo if d not in done]
            if not todo:
                break
            for doc_uuid in todo:
                async with sm() as s:
                    ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
                    doc = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
                    if ds is None or doc is None:
                        continue
                    segments = list(
                        (
                            await s.scalars(
                                select(Segment)
                                .where(Segment.document_uuid == doc_uuid)
                                .order_by(Segment.position)
                            )
                        ).all()
                    )
                    points += await index_segments(s, rt, ds, doc, segments, targets=[target])
                    await s.commit()
                done.add(doc_uuid)
                status = await _progress(sm, vid, {"documents": len(done), "points": points})
                if status != "building":  # 构建中被删掉了
                    return
        # 清理：构建期间被删掉的分段 / 文档留下的 point
        async with sm() as s:
            ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
            valid = await _valid_point_ids(s, ds) if ds is not None else set()
        orphans = [pid for pid in await store.point_ids() if pid not in valid]
        await store.delete_by_ids(orphans)
    except Exception as exc:
        logger.exception("索引版本构建失败 %s", version_uuid)
        async with sm() as s:
            version = await s.scalar(select(IndexVersion).where(IndexVersion.uuid == vid))
            if version is not None:
                version.status = "failed"
                version.error = str(exc)[:2000]
                await s.commit()
        return

    async with sm() as s:
        version = await s.scalar(select(IndexVersion).where(IndexVersion.uuid == vid))
        ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
        if version is None or ds is None or version.status != "building":
            return
        version.status = "ready"
        version.progress = {
            **(version.progress or {}),
            "documents": len(done),
            "points": points,
            "orphans_removed": len(orphans),
        }
        if version.auto_activate:
            await activate(s, ds, version)
        await s.commit()


async def _progress(
    sm: async_sessionmaker[AsyncSession], vid: UUID, data: dict[str, Any]
) -> str | None:
    async with sm() as s:
        version = await s.scalar(select(IndexVersion).where(IndexVersion.uuid == vid))
        if version is None:
            return None
        version.progress = {**(version.progress or {}), **data}
        await s.commit()
        return version.status
