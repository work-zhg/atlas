"""租户接入：宿主要实现的类 TenantLifecycle（创建、删除）。

    创建   开通租户（或给新登记的系统补建）时调：新租户在本系统要先准备的东西（份额、默认数据）。
          在这个租户的上下文里执行，参数是运营中心开通时给本系统填的。
    删除   关闭租户时调，分批：先 delete_external 删数据库之外的东西（对象存储、向量、Pod），
          外部删完再按 purge_order 在 OPS 系统范围里分批删行；返回删了多少、还有没有剩。

★ 业务系统不知道租户状态：没有停用、恢复，也不查状态。只能删已停用的租户由运营中心保证；
  租户停用、到期后，用户中心对这个租户的人一律返回无权限。
★ 创建、删除都必须能重做：运营中心超时后会重调同一步。创建按租户 uuid 写数据，再建一次结果一样；
  删除每次删剩下的，删完返回 remaining=false。所以本库不按幂等键去重，也不记运营请求。
★ 先删外部、后删数据库：数据库里的行是找到文件、向量集合、Pod 的线索。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from atlas_tenancy import PurgeReport, SystemReason, TenantDataPurge, TenantOwned, TenantScope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["Rejected", "TenantLifecycle"]


class Rejected(Exception):
    """前置条件不满足或参数错：重做也一样，要人看（运营接口回 409，运营中心按「被拒绝」处理）。"""


class TenantLifecycle(ABC):
    """宿主继承它，写 create，给出 purge_order；数据库之外还有东西要删的再覆盖 delete_external。

    class KnowledgeTenantLifecycle(TenantLifecycle):
        purge_order = (ChildChunk, Segment, …, Dataset)   # 子表在前

        async def create(self, tenant_uuid, params): …      # 记存储份额
        async def delete_external(self, tenant_uuid, budget): …   # 删 collection、文件
    """

    #: 本系统全部租户表的模型类，子表在前（本仓不建外键，顺序由宿主显式给出）
    purge_order: ClassVar[Sequence[type[TenantOwned]]] = ()

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        base: type | None = None,
        budget: int = 5000,
    ) -> None:
        """base：宿主的 DeclarativeBase，给了就核对 purge_order 没有漏掉任何租户表。
        budget：一次删除最多删多少（外部资源个数、数据库行数各算一份）。"""
        self._sessions = sessions
        self._purge = TenantDataPurge(self.purge_order, base=base) if self.purge_order else None
        self.budget = budget

    @abstractmethod
    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        """新租户在本系统要先准备的东西。在这个租户的上下文里调；必须能重做。
        params 不认识或不合法时抛 Rejected。"""

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        """删数据库之外的东西，每次最多 budget 个。没有外部资源的系统不用覆盖。"""
        return PurgeReport()

    async def delete(self, tenant_uuid: UUID) -> PurgeReport:
        """删一批：先外部（在这个租户的上下文里），外部删完再删数据库（在 OPS 系统范围里）。"""
        with TenantScope.enter(tenant_uuid):
            report = await self.delete_external(tenant_uuid, self.budget)
        if report.remaining or self._purge is None:
            return report
        async with self._sessions() as session:
            with TenantScope.system(SystemReason.OPS):
                report.merge(await self._purge.batch(session, tenant_uuid, self.budget))
            await session.commit()
        return report
