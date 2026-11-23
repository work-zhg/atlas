"""知识库文件的存储份额（多租户设计 §9；c4-model/opscenter/code-gateway.html · CapacityApi）。

三层里知识库只有租户这一层：知识库是租户里大家共用的，没有每人上限。

    份额   运营中心给某个租户单独定的（kb_tenant_capacity）；没定过的用默认份额
           （系统参数 capacity.default_storage_mb，经应用配置库改，有修订历史）
    用量   这个租户全部文档的原文件大小之和（kb_document.file_size）
    判断   上传前：已用 + 这次的 > 份额 → 拒绝，并说明是租户份额满了

★ 只有数字，不含任何租户的内容。份额 None = 不限。
★ 两个上传同时进来可能一起越过份额一点点（没有锁）：存储份额是软上限，下一次上传就挡住了。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from atlas_appconfig import AppConfig, ConfigKey
from atlas_tenancy import TenantScope
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Document, TenantCapacity
from ..errors import Conflict

__all__ = [
    "CONFIG_KEYS",
    "DEFAULT_STORAGE_MB",
    "KnowledgeCapacity",
    "StorageQuotaExceeded",
    "TenantStorage",
]

MB = 1024 * 1024

DEFAULT_STORAGE_MB = ConfigKey(
    "capacity.default_storage_mb",
    int,
    default=0,
    description="没单独定份额的租户：知识库文件的存储（MB，0 = 不限）",
)
#: 本服务在应用配置库里登记的全部 key（系统参数，只有运营中心能改）
CONFIG_KEYS = [DEFAULT_STORAGE_MB]


class StorageQuotaExceeded(Conflict):
    """租户的知识库存储份额满了。"""


@dataclass(frozen=True)
class TenantStorage:
    tenant_uuid: UUID
    #: None = 不限
    quota_bytes: int | None
    #: 没单独定过，用的是默认份额
    is_default: bool
    used_bytes: int


class KnowledgeCapacity:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.config = AppConfig(sessions, CONFIG_KEYS)

    async def default_bytes(self) -> int | None:
        mb = await self.config.get(DEFAULT_STORAGE_MB)
        return mb * MB if mb > 0 else None

    async def usage(self, session: AsyncSession) -> TenantStorage:
        """当前租户的份额和用量。"""
        row = await session.scalar(select(TenantCapacity))
        used = await session.scalar(select(func.coalesce(func.sum(Document.file_size), 0)))
        quota = row.storage_bytes if row is not None else await self.default_bytes()
        return TenantStorage(TenantScope.require(), quota, row is None, int(used or 0))

    async def check(self, session: AsyncSession, incoming: int) -> None:
        u = await self.usage(session)
        if u.quota_bytes is not None and u.used_bytes + incoming > u.quota_bytes:
            raise StorageQuotaExceeded(
                "本租户的知识库存储份额已满（租户份额）："
                f"已用 {_mb(u.used_bytes)}，这次 {_mb(incoming)}，份额 {_mb(u.quota_bytes)}",
                used_bytes=u.used_bytes,
                quota_bytes=u.quota_bytes,
            )

    async def set_quota(self, session: AsyncSession, storage_bytes: int | None, *, by: str) -> None:
        """单独给当前租户定份额（None = 不限）。"""
        row = await session.scalar(select(TenantCapacity))
        if row is None:
            row = TenantCapacity()
            session.add(row)
        row.storage_bytes = storage_bytes
        row.updated_by = by[:64]
        await session.flush()

    async def use_default(self, session: AsyncSession) -> None:
        """回到默认份额（之后改默认份额照样跟着变）。"""
        row = await session.scalar(select(TenantCapacity))
        if row is not None:
            await session.delete(row)
            await session.flush()


def _mb(n: int) -> str:
    return f"{n / MB:.1f} MB"
