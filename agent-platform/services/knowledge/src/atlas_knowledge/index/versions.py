"""索引版本（doc/knowledge-enterprise-design.html §6.2）：双写目标、重建、原子切换。

    active_version  库当前在用的版本（检索、语义分段用它的模型）。每个库恰好一个
    write_targets   写向量时的目标：active 版本 + 其余 collection 还在的版本（构建中、待切换、
                    已退役但保留用于回滚）。分段内容只有一份，各版本只是向量不同
    stores          删除 / 开关 / payload 变更要同步到的所有 collection
    rebuild         后台任务：用新模型为所有已完成的文档重新向量化，写进新 collection
    activate        原子切换：一个事务里改版本状态（模型和 collection 只记在版本上）

★ 构建期间新入库的文档：入库每一批写向量前读一次 write_targets，构建中的版本也在里面（双写）。
  版本建立这一刻就在写向量的文档（early_documents，建版本时记下）前几批没进新 collection：
  重建等它们停下（完成、暂停或失败）后再补写，最后删掉新 collection 里库中已不存在的 point。
★ 重建持锁、按文档记进度（cursor）：进程死了由巡检（recover_rebuilds）续跑，不会卡在 building。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas_redislock import LockBusy, LockLost, RedisLock
from atlas_tenancy import SystemReason, TenantScope, TenantSweep
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
from ..retrieval.filters import field_index_kind
from ..runtime import Runtime
from .vdb import BaseVector

__all__ = [
    "Target",
    "activate",
    "active_version",
    "active_versions",
    "early_documents",
    "ensure_collection",
    "live_versions",
    "rebuild",
    "rebuild_lock_key",
    "rebuild_queued",
    "recover_rebuilds",
    "stores",
    "target_of",
    "write_targets",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    collection: str
    provider: str
    model: str
    dim: int


def target_of(v: IndexVersion) -> Target:
    return Target(v.collection_name, v.embedding_provider, v.embedding_model, v.embedding_dim)


async def active_versions(
    session: AsyncSession, dataset_uuids: list[UUID]
) -> dict[UUID, IndexVersion]:
    """库 → 它的 active 版本。"""
    if not dataset_uuids:
        return {}
    rows = await session.scalars(
        select(IndexVersion).where(
            IndexVersion.dataset_uuid.in_(dataset_uuids), IndexVersion.status == "active"
        )
    )
    return {v.dataset_uuid: v for v in rows.all()}


async def active_version(session: AsyncSession, dataset_uuid: UUID) -> IndexVersion:
    found = (await active_versions(session, [dataset_uuid])).get(dataset_uuid)
    if found is None:  # 建库时登记第 1 版、切换在一个事务里：不该出现
        raise RuntimeError(f"知识库 {dataset_uuid} 没有在用的索引版本")
    return found


async def live_versions(session: AsyncSession, ds: Dataset) -> list[IndexVersion]:
    """collection 还在的版本，active 在前。"""
    rows = await session.scalars(
        select(IndexVersion)
        .where(
            IndexVersion.dataset_uuid == ds.uuid,
            IndexVersion.status.in_(LIVE_INDEX_STATUSES),
        )
        .order_by(IndexVersion.version)
    )
    return sorted(rows.all(), key=lambda v: v.status != "active")


async def write_targets(session: AsyncSession, ds: Dataset) -> list[Target]:
    return [target_of(v) for v in await live_versions(session, ds)]


async def ensure_collection(
    session: AsyncSession, rt: Runtime, target: Target, dataset_uuid: UUID
) -> BaseVector:
    """collection 的读写对象；第一次用时建好，带上库里全部元数据字段的路径索引。

    ★ 字段索引要和 collection 一起建：MetadataFieldDefined 只能给当时已有的 collection 补索引，
      之后才建的（第一次入库、重建出来的新版本）没有它，按元数据过滤就退化成全量扫描。
    """
    from ..dataset.metadata import fields_of

    store = rt.vectors.for_collection(target.collection)
    if await store.create(target.dim):
        for name, f in (await fields_of(session, [dataset_uuid]))[str(dataset_uuid)].items():
            await store.ensure_index(f"meta.{name}", field_index_kind(f))
    return store


async def stores(session: AsyncSession, rt: Runtime, ds: Dataset) -> list[BaseVector]:
    return [rt.vectors.for_collection(t.collection) for t in await write_targets(session, ds)]


async def activate(session: AsyncSession, ds: Dataset, version: IndexVersion) -> None:
    """切换（调用方提交事务）。旧的 active 版本退役，collection 保留用于回滚。"""
    if version.status not in ("ready", "retired", "active"):
        raise ValueError(f"版本状态为 {version.status}，不能切换")
    for v in await live_versions(session, ds):
        if v.status == "active" and v.uuid != version.uuid:
            v.status = "retired"
    # ★ 先把旧的退役写下去：每个库只能有一个 active（部分唯一索引），回滚到旧版本时它的 id 更小，
    #   同一次 flush 里会先改它
    await session.flush()
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


async def rebuild_queued(
    rt: Runtime, sm: async_sessionmaker[AsyncSession], version_uuid: str
) -> bool:
    """worker 的入口：进入索引版本所属的租户再重建（租户从版本这一行取）。

    租户已停用的不开始。→ 是不是因为租户停用没做。
    """
    suspended = False

    async def fetch() -> list[tuple[str, UUID]]:
        async with sm() as s:
            tenant = await s.scalar(
                select(IndexVersion.tenant_uuid).where(IndexVersion.uuid == UUID(version_uuid))
            )
        return [] if tenant is None else [(version_uuid, tenant)]

    async def handle(vid: str) -> None:
        nonlocal suspended
        if not await rt.gate.is_active(TenantScope.require()):
            logger.info("索引版本 %s 所在的租户已停用，暂不重建", vid)
            suspended = True
            return
        await rebuild(rt, sm, vid)

    await TenantSweep.each(fetch, handle)
    return suspended


#: 重建锁的 TTL。不是工作时长上限：持有期间看门狗每 TTL/3 续一次，它只决定进程死后锁空悬多久
REBUILD_LOCK_TTL_S = 60.0
#: 入库编排写向量的状态（和 runner.IN_PROGRESS 一致，这里不 import runner 免得循环）
_WRITING = ("parsing", "splitting", "indexing")


def rebuild_lock_key(version_uuid: str | UUID) -> str:
    return f"kb:index_version:{version_uuid}:rebuild"


async def early_documents(session: AsyncSession, dataset_uuid: UUID) -> list[str]:
    """新版本建立这一刻就在写向量的文档：之前写的批次没进新 collection，要等它们完成后整篇重写。

    = 正在向量化的（可能有一批读了旧的写入目标、还没提交），加上没完成却已有分段写过向量的
    （暂停、失败、排队中的续跑）。
    """
    written = select(Segment.id).where(
        Segment.document_uuid == Document.uuid, Segment.status == "completed"
    )
    rows = await session.scalars(
        select(Document.uuid).where(
            Document.dataset_uuid == dataset_uuid,
            Document.indexing_status != "completed",
            (Document.indexing_status == "indexing") | written.exists(),
        )
    )
    return [str(u) for u in rows.all()]


async def recover_rebuilds(rt: Runtime, sm: async_sessionmaker[AsyncSession]) -> list[str]:
    """巡检：处在 building、却没有任务持锁的版本重新投递，从记下的进度续跑。→ 投递了哪些。

    ★ 重复投递无害：多出来的任务拿不到锁就跳过，版本已不在 building 就直接返回。
    """
    redis = rt.redis
    if redis is None:
        return []
    with TenantScope.system(SystemReason.SWEEP):
        async with sm() as s:
            rows = (
                await s.execute(
                    select(IndexVersion.uuid, IndexVersion.tenant_uuid).where(
                        IndexVersion.status == "building"
                    )
                )
            ).all()
    stalled = [
        str(u)
        for u, tenant in rows
        if not await redis.exists(rebuild_lock_key(u)) and await rt.gate.is_active(tenant)
    ]
    for vid in stalled:
        rt.dispatcher.rebuild_index(vid)
    return stalled


async def rebuild(rt: Runtime, sm: async_sessionmaker[AsyncSession], version_uuid: str) -> None:
    """后台重建一个版本：持锁跑，进度记在版本上，进程死了由巡检续跑（recover_rebuilds）。"""
    vid = UUID(version_uuid)
    if rt.redis is None:
        await _rebuild(rt, sm, vid)
        return
    try:
        async with RedisLock(rt.redis, rebuild_lock_key(vid), ttl_s=REBUILD_LOCK_TTL_S).hold():
            await _rebuild(rt, sm, vid)
    except LockBusy:
        logger.info("索引版本 %s 已有重建任务在跑，跳过", vid)
    except LockLost:
        logger.warning("索引版本 %s 重建中途失去锁，已中断（巡检会续上）", vid)


async def _rebuild(rt: Runtime, sm: async_sessionmaker[AsyncSession], vid: UUID) -> None:
    """① 建 collection（带全部元数据字段的索引）→ ② 按 id 逐个重写已完成的文档，每个文档记一次
    进度（cursor）→ ③ 版本建立前就在写的文档：还在写就先返回、等巡检再来；已停下的把写过的分段补进来
    → ④ 删掉库里已不存在的 point → ready（auto_activate 的直接切换）。

    ★ 其余文档不用管：版本建立之后才开始写向量的，每一批都会写进它（双写）。
    """
    async with sm() as s:
        version = await s.scalar(select(IndexVersion).where(IndexVersion.uuid == vid))
        if version is None or version.status != "building":
            return
        ds_uuid = version.dataset_uuid
        target = target_of(version)
        progress = dict(version.progress or {})
        store = await ensure_collection(s, rt, target, ds_uuid)
        await s.commit()
    cursor = int(progress.get("cursor") or 0)
    points = int(progress.get("points") or 0)
    count = int(progress.get("documents") or 0)
    early = [str(u) for u in progress.get("early") or []]
    try:
        # ② 已完成的文档按 id 顺序重写；中途中断从 cursor 之后续上
        while True:
            async with sm() as s:
                batch = (
                    await s.execute(
                        select(Document.id, Document.uuid)
                        .where(
                            Document.dataset_uuid == ds_uuid,
                            Document.indexing_status == "completed",
                            Document.id > cursor,
                        )
                        .order_by(Document.id)
                        .limit(50)
                    )
                ).all()
            if not batch:
                break
            for doc_id, doc_uuid in batch:
                points += await _rewrite(rt, sm, ds_uuid, doc_uuid, target)
                cursor, count = doc_id, count + 1
                early = [u for u in early if u != str(doc_uuid)]
                if (
                    await _progress(
                        sm,
                        vid,
                        {"cursor": cursor, "documents": count, "points": points, "early": early},
                    )
                    != "building"
                ):  # 构建中被删掉了
                    return
        # ③ 版本建立前就在写的文档
        waiting: list[str] = []
        for doc_uuid in early:
            async with sm() as s:
                status = await s.scalar(
                    select(Document.indexing_status).where(Document.uuid == UUID(doc_uuid))
                )
            if status is None:  # 已删
                continue
            if status in _WRITING:
                waiting.append(doc_uuid)
            elif status == "completed":  # 在 ② 之后才完成：整篇重写
                points += await _rewrite(rt, sm, ds_uuid, UUID(doc_uuid), target)
            else:  # 暂停、失败、排队中：把已写过向量的分段补进来，剩下的续跑时会双写
                points += await _rewrite(rt, sm, ds_uuid, UUID(doc_uuid), target, done_only=True)
        early = waiting
        if await _progress(sm, vid, {"points": points, "early": early}) != "building":
            return
        if early:
            logger.info("索引版本 %s 等 %d 个文档写完再收尾", vid, len(early))
            return
        # ④ 清理：构建期间被删掉的分段 / 文档留下的 point
        async with sm() as s:
            ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
            valid = await _valid_point_ids(s, ds) if ds is not None else set()
        orphans = [pid for pid in await store.point_ids() if pid not in valid]
        await store.delete_by_ids(orphans)
    except Exception as exc:
        logger.exception("索引版本构建失败 %s", vid)
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
        version.progress = {**(version.progress or {}), "orphans_removed": len(orphans)}
        if version.auto_activate:
            await activate(s, ds, version)
        await s.commit()


async def _rewrite(
    rt: Runtime,
    sm: async_sessionmaker[AsyncSession],
    ds_uuid: UUID,
    doc_uuid: UUID,
    target: Target,
    *,
    done_only: bool = False,
) -> int:
    """用新版本的模型重写一个文档的向量（只写这个版本）。done_only：只写已写过向量的分段。"""
    from .loader import index_segments

    async with sm() as s:
        ds = await s.scalar(select(Dataset).where(Dataset.uuid == ds_uuid))
        doc = await s.scalar(select(Document).where(Document.uuid == doc_uuid))
        if ds is None or doc is None:
            return 0
        q = select(Segment).where(Segment.document_uuid == doc_uuid)
        if done_only:
            q = q.where(Segment.status == "completed")
        segments = list((await s.scalars(q.order_by(Segment.position))).all())
        # ★ 只往新版本写，不改分段的状态：done_only 时其余分段还等着入库编排续跑
        statuses = {seg.uuid: seg.status for seg in segments}
        n = await index_segments(s, rt, ds, doc, segments, targets=[target])
        for seg in segments:
            seg.status = statuses[seg.uuid]
        await s.commit()
        return n


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
