"""索引版本管理（doc/knowledge-enterprise-design.html §6.2）：换向量模型不停服。

    create     新建版本（building）→ 投递后台重建；构建期间入库双写
    activate   原子切换（也用于回滚到保留着的旧版本）
    delete     删掉非 active 版本的 collection

★ 构建完成前，检索一直用旧版本；auto_activate 的版本构建完成后自动切换。
★ 旧版本退役后 collection 保留并继续双写（回滚后数据是全的），直到手动删除。
★ 订阅知识库的事件（wiring.py）：建库登记第 1 版、删库删所有 collection、加字段给每个
  collection 的 meta 建路径索引。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access.permission import DatasetAccess
from ..dataset.events import DatasetCreated, DatasetDeleted, MetadataFieldDefined
from ..db.models import LIVE_INDEX_STATUSES, Dataset, IndexVersion
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..retrieval.filters import field_index_kind
from ..runtime import Runtime
from . import versions
from .vdb import collection_name

__all__ = ["IndexVersionService", "on_dataset_created", "on_dataset_deleted", "on_field_defined"]


# ───────────────────────────── 知识库事件的订阅方


async def on_dataset_created(session: AsyncSession, rt: Runtime, ev: DatasetCreated) -> None:
    """登记第 1 版（建库时选的模型）。collection 在第一次写向量时建。"""
    session.add(
        IndexVersion(
            dataset_uuid=ev.dataset.uuid,
            version=1,
            embedding_provider=ev.embedding_provider,
            embedding_model=ev.embedding_model,
            embedding_dim=ev.embedding_dim,
            collection_name=collection_name(ev.dataset.uuid),
            status="active",
            activated_at=utcnow(),
            created_by=ev.actor,
        )
    )
    await session.flush()


async def on_dataset_deleted(session: AsyncSession, rt: Runtime, ev: DatasetDeleted) -> None:
    """删所有 collection 还在的版本，再删版本行。"""
    for v in await versions.live_versions(session, ev.dataset):
        await rt.vectors.for_collection(v.collection_name).delete()
    await session.execute(delete(IndexVersion).where(IndexVersion.dataset_uuid == ev.dataset.uuid))


async def on_field_defined(session: AsyncSession, rt: Runtime, ev: MetadataFieldDefined) -> None:
    """给每个 collection 还在的版本建这个字段的路径索引（还没建的 collection 建的时候补上）。"""
    rows = await session.scalars(
        select(IndexVersion).where(
            IndexVersion.dataset_uuid == ev.dataset_uuid,
            IndexVersion.status.in_(LIVE_INDEX_STATUSES),
        )
    )
    for v in rows.all():
        await rt.vectors.for_collection(v.collection_name).ensure_index(
            f"meta.{ev.field.name}", field_index_kind(ev.field)
        )


class IndexVersionService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    async def list(self, ds: Dataset) -> list[IndexVersion]:
        rows = await self._s.scalars(
            select(IndexVersion)
            .where(IndexVersion.dataset_uuid == ds.uuid)
            .order_by(IndexVersion.version.desc())
        )
        return list(rows.all())

    async def get(self, ds: Dataset, version_uuid: UUID) -> IndexVersion:
        v = await self._s.scalar(
            select(IndexVersion).where(
                IndexVersion.dataset_uuid == ds.uuid, IndexVersion.uuid == version_uuid
            )
        )
        if v is None:
            raise NotFound("索引版本不存在")
        return v

    async def create(
        self,
        access: DatasetAccess,
        *,
        provider: str,
        model: str,
        auto_activate: bool = False,
    ) -> IndexVersion:
        """新建版本并投递重建。调用方不用再提交：这里先提交，worker 才看得到版本。"""
        access.require_admin()
        ds = access.ds
        building = await self._s.scalar(
            select(func.count())
            .select_from(IndexVersion)
            .where(IndexVersion.dataset_uuid == ds.uuid, IndexVersion.status == "building")
        )
        if building:
            raise Conflict("已有正在构建的索引版本，请等它完成或删除后再建")
        impl = await asyncio.to_thread(self._rt.registry.embedding, provider, model)
        last = int(
            await self._s.scalar(
                select(func.coalesce(func.max(IndexVersion.version), 0)).where(
                    IndexVersion.dataset_uuid == ds.uuid
                )
            )
            or 0
        )
        number = last + 1
        v = IndexVersion(
            dataset_uuid=ds.uuid,
            version=number,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dim=impl.dim,
            collection_name=f"{collection_name(ds.uuid)}_v{number}",
            status="building",
            auto_activate=auto_activate,
            # 这一刻就在写向量的文档：前几批没进新 collection，重建等它们停下再补
            progress={
                "documents": 0,
                "points": 0,
                "early": await versions.early_documents(self._s, ds.uuid),
            },
            created_by=access.p.uid,
        )
        self._s.add(v)
        await self._s.commit()
        self._rt.dispatcher.rebuild_index(str(v.uuid))
        return v

    async def activate(self, access: DatasetAccess, v: IndexVersion) -> IndexVersion:
        access.require_admin()
        if v.status == "active":
            return v
        if v.status not in ("ready", "retired"):
            raise Invalid(f"版本状态为 {v.status}，构建完成（ready）或已退役的版本才能切换")
        await versions.activate(self._s, access.ds, v)
        await self._s.flush()
        return v

    async def delete(self, access: DatasetAccess, v: IndexVersion) -> None:
        access.require_admin()
        if v.status == "active":
            raise Invalid("不能删除正在使用的版本")
        if v.status == "deleted":
            return
        await self._rt.vectors.for_collection(v.collection_name).delete()
        v.status = "deleted"
        await self._s.flush()
