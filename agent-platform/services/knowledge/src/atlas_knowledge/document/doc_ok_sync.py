"""修数据：按库里的文档状态重写向量库的 doc_ok（去掉入库审核之后要跑一次）。

去掉入库审核（迁移 k0014）时，库里的状态由迁移改好：待审核的文档放开（可被检索只看启用、
归档），已驳回的改成停用。向量库里的 doc_ok 还是旧值 —— 原来待审核的仍是 false，检索不到。
迁移碰不到向量库，所以单独一条命令：

    python -m atlas_knowledge sync-doc-ok

做法：逐个库（进入它的租户），每份文档按 Document.available 写一次 doc_ok，写进这个库全部要写的
索引版本；向量库按 document_uuid 批量改，不重新向量化。

★ 能重做：结果只取决于库里的状态。一份文档一次请求（每个索引版本各一次）。
★ 也是修复工具：怀疑向量库的 doc_ok 和库里对不上时再跑一次。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from atlas_tenancy import TenantSweep
from sqlalchemy import select

from ..db.models import Dataset, Document
from ..index.versions import stores

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..runtime import Runtime

__all__ = ["sync_doc_ok"]


async def sync_doc_ok(rt: Runtime, sm: async_sessionmaker[AsyncSession]) -> int:
    """→ 同步了几个库。"""

    async def fetch() -> list[tuple[UUID, UUID]]:
        async with sm() as s:
            rows = await s.execute(select(Dataset.uuid, Dataset.tenant_uuid))
            return [(u, t) for u, t in rows]

    async def handle(dataset_uuid: UUID) -> None:
        async with sm() as s:
            ds = await s.scalar(select(Dataset).where(Dataset.uuid == dataset_uuid))
            if ds is None:
                return
            targets = await stores(s, rt, ds)
            docs = (await s.scalars(select(Document).where(Document.dataset_uuid == ds.uuid))).all()
            for doc in docs:
                for store in targets:
                    await store.set_doc_ok(str(doc.uuid), doc.available)

    ok, _failed = await TenantSweep.each(fetch, handle)
    return ok
