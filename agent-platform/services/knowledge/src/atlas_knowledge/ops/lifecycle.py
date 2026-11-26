"""知识库的租户接入：创建、删除（运营接口 PUT / DELETE /ops/v1/tenants/{tenant}，api/ops.py）。

★ 创建：开通时运营中心给知识库填了存储份额（storage_bytes）就记下；没填不建行，用默认份额
  （系统参数 capacity.default_storage_mb）。能重做：再记一次结果一样。
★ 不知道租户状态：停用、恢复只在用户中心；停用后用户中心对这个租户的人回答无权限。
★ 删除：先删外部资源 —— 向量库里这个租户的 collection、对象存储里这个租户的文件 —— 再分批删
  数据库里的行。对象只删知识库自己的子目录（{租户}/_knowledge/），不删整个 {租户}/：桶和 Atlas
  的工作空间、技能共用。多租户之前建的库在桶根 _knowledge/{库} 下，按库记着的目录逐个删。
  用户中心里这些库的授权随租户在用户中心一起删。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from atlas_opsapi import Rejected, TenantLifecycle
from atlas_tenancy import PurgeReport, TenantNaming
from fastapi import FastAPI
from sqlalchemy import select

from ..db.models import (
    LIVE_INDEX_STATUSES,
    Base,
    ChildChunk,
    Dataset,
    Document,
    EnhanceCacheEntry,
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
from .capacity import KnowledgeCapacity

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
    EnhanceCacheEntry,
    TenantCapacity,
)


class KnowledgeTenantLifecycle(TenantLifecycle):
    """知识库的租户接入。create、delete_external 都在这个租户的上下文里调（运营接口库）。"""

    purge_order = PURGE_ORDER

    def __init__(self, app: FastAPI, capacity: KnowledgeCapacity) -> None:
        super().__init__(get_sessionmaker(), base=Base)
        self._app = app
        self._capacity = capacity

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        if "storage_bytes" not in params:
            return
        quota = params["storage_bytes"]
        if quota is not None and (
            not isinstance(quota, int) or isinstance(quota, bool) or quota < 0
        ):
            raise Rejected("storage_bytes 必须是非负整数，或 null（不限）")
        async with self._sessions() as session:
            await self._capacity.set_quota(session, quota, by="运营中心（开通租户）")
            await session.commit()

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        rt: Runtime = self._app.state.runtime
        report = PurgeReport()
        async with self._sessions() as session:
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
