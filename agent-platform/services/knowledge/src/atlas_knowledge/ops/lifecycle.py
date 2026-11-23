"""知识库的租户生命周期：停用、恢复、删除（运营接口 /ops/v1/tenants/{tenant}/…，api/ops.py）。

★ 停用：管理接口、内部接口、MCP 一律 403；入库、重建开始前查租户状态，停用的不开始，过一会儿再
  看（tasks/indexing.py），恢复后接着入。正在入的那一份做完为止。
★ 删除：先删外部资源 —— 向量库里这个租户的 collection、对象存储里这个租户的文件 —— 再分批删
  数据库里的行。对象只删知识库自己的子目录（{租户}/_knowledge/），不删整个 {租户}/：桶和 Atlas
  的工作空间、技能共用。多租户之前建的库在桶根 _knowledge/{库} 下，按库记着的目录逐个删。
  用户中心里这些库的授权随租户在用户中心一起删。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from atlas_tenancy import PurgeReport, TenantNaming
from fastapi import FastAPI
from sqlalchemy import select

from ..db.models import (
    LIVE_INDEX_STATUSES,
    ChildChunk,
    Dataset,
    Document,
    IndexVersion,
    KnowledgeSet,
    MetadataField,
    ProcessRule,
    QueryLog,
    Retrieval,
    Segment,
    TenantCapacity,
)
from ..db.session import get_sessionmaker
from ..document.storage import StorageNotConfigured
from ..runtime import Runtime

__all__ = ["PURGE_ORDER", "KnowledgeTenantLifecycle"]

#: 删除租户时按这个顺序分批删（子表在前）
PURGE_ORDER = (
    ChildChunk,
    Segment,
    QueryLog,
    Retrieval,
    MetadataField,
    IndexVersion,
    Document,
    ProcessRule,
    Dataset,
    KnowledgeSet,
    TenantCapacity,
)


class KnowledgeTenantLifecycle:
    """知识库的租户生命周期。钩子都在这个租户的上下文里调（TenantLifecycleService）。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        return None

    async def on_resume(self, tenant_uuid: UUID) -> None:
        return None

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        rt: Runtime = self._app.state.runtime
        report = PurgeReport()
        async with get_sessionmaker()() as session:
            datasets = list(await session.scalars(select(Dataset)))
            collections = set(
                await session.scalars(
                    select(IndexVersion.collection_name).where(
                        IndexVersion.status.in_(LIVE_INDEX_STATUSES)
                    )
                )
            )
        for name in sorted(collections):
            await rt.vectors.for_collection(name).delete()
        if collections:
            report.deleted["collection"] = len(collections)

        own = TenantNaming.object_prefix(rt.settings.oss_prefix) + "/"
        legacy = [f"{d.storage_prefix}/" for d in datasets if not d.storage_prefix.startswith(own)]
        try:
            storage = rt.storage
        except StorageNotConfigured:
            return report  # 没配对象存储：没有文件要删

        def _delete() -> int:
            return sum(storage.delete_prefix(prefix) for prefix in [own, *legacy])

        deleted = await asyncio.to_thread(_delete)
        if deleted:
            report.deleted["object"] = deleted
        return report
