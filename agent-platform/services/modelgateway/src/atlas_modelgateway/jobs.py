"""管理平台的定时任务（atlas_jobs 的实例任务，多副本各自跑，做的事都幂等）。

    AccountCheck     每 5 分钟：网关标了异常的账号（余额不足、密钥失效）测一次，好了就解除；
                     能查余额的账号顺带查余额，低于提醒线的在账号列表顶部标出来
    KeyRefresh       每 5 分钟：重算用户 Key 的有效模型 —— 用户中心那边的变化（调部门、被停用、
                     授权变了）没有通知，只能定期重算
    CallRetention    每天：调用记录保留期之外的分区整块删掉

★ 跨租户的固定写法（TenantSweep）：先在系统范围里只取 (待办, 租户)，再逐条进入各自的租户处理。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from atlas_jobs import Job, JobContext, NoParams
from atlas_tenancy import TenantSweep
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import notify
from .accounts import AccountService
from .auth import use_outbound
from .crypto import SecretCipher
from .db.models import GwAccount, GwApiKey
from .gateway.format import AddressGuard, UpstreamClient
from .gateway.metering import PartitionKeeper
from .gateway.routing import RouteHealth
from .keys import KeyService
from .perms import Permissions, SubjectsCache
from .provider import ProviderCatalog
from .registry import ModelRegistry

__all__ = ["AccountCheck", "CallRetention", "KeyRefresh"]

logger = logging.getLogger(__name__)


class AccountCheck(Job):
    type = "modelgateway.account_check"
    description = "检查异常账号、查余额"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        cipher: SecretCipher,
        catalog: ProviderCatalog,
        guard: AddressGuard,
        health: RouteHealth,
        client: UpstreamClient,
    ) -> None:
        self._sm = sessionmaker
        self._cipher = cipher
        self._catalog = catalog
        self._guard = guard
        self._health = health
        self._client = client

    def _service(self, session: AsyncSession) -> AccountService:
        return AccountService(
            session,
            cipher=self._cipher,
            catalog=self._catalog,
            guard=self._guard,
            health=self._health,
        )

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        abnormal = set(await self._health.abnormal_accounts())

        async def fetch() -> list[tuple[UUID, UUID]]:
            async with self._sm() as session:
                rows = await session.execute(
                    select(
                        GwAccount.uuid, GwAccount.tenant_uuid, GwAccount.provider, GwAccount.enabled
                    )
                )
                return [
                    (uuid, tenant)
                    for uuid, tenant, provider, enabled in rows
                    if enabled and (uuid in abnormal or self._catalog.spec(provider).balance_url)
                ]

        async def handle(account_uuid: UUID) -> None:
            async with self._sm() as session:
                service = self._service(session)
                row = await service.get(account_uuid)
                ref = service.ref(row)
                if account_uuid in abnormal:
                    result = await service.probe(ref, self._client)
                    await service.record_check(row, result)
                    if result.ok:
                        logger.info("账号 %s（%s）恢复了，解除异常", row.name, row.uuid)
                else:
                    balance = await self._catalog.dialect(row.provider).balance(
                        ref, self._client.client(ref.egress)
                    )
                    if balance is not None:
                        row.balance, row.balance_at = balance, datetime.now(UTC)
                        if row.balance_alert is not None and balance < row.balance_alert:
                            logger.warning(
                                "账号 %s 余额 %s 低于提醒线 %s",
                                row.name,
                                balance,
                                row.balance_alert,
                            )
                await session.commit()

        ok, failed = await TenantSweep.each(fetch, handle)
        if failed:
            logger.warning("检查账号：%d 个成功，%d 个失败", ok, failed)


class KeyRefresh(Job):
    type = "modelgateway.key_refresh"
    description = "重算用户 Key 的有效模型"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        perms: Permissions,
        subjects: SubjectsCache,
        cipher: SecretCipher,
    ) -> None:
        self._sm = sessionmaker
        self._perms = perms
        self._subjects = subjects
        self._cipher = cipher

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        async def fetch() -> list[tuple[tuple[UUID, UUID | None], UUID]]:
            async with self._sm() as session:
                rows = await session.execute(
                    select(GwApiKey.uuid, GwApiKey.user_uuid, GwApiKey.tenant_uuid).where(
                        GwApiKey.key_type == "user"
                    )
                )
                return [((k, u), t) for k, u, t in rows]

        async def handle(item: tuple[UUID, UUID | None]) -> None:
            key_uuid, user = item
            # 没有入站请求：按持有人调用户中心（租户由当前上下文写进请求头）
            use_outbound({"X-Uid": str(user)} if user else None)
            async with self._sm() as session:
                keys = KeyService(
                    session, ModelRegistry(session, self._perms), self._subjects, self._cipher
                )
                key = await keys.get(key_uuid)
                await keys.refresh(key, fresh=True)
                await session.commit()
                await notify.flush(session)
            use_outbound(None)

        await TenantSweep.each(fetch, handle)


class CallRetention(Job):
    type = "modelgateway.call_retention"
    description = "删掉保留期之外的调用记录"

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], months: int) -> None:
        self._keeper = PartitionKeeper(sessionmaker)
        self._months = months

    async def execute(self, ctx: JobContext, params: Any) -> None:
        cutoff = datetime.now(UTC).replace(day=1) - timedelta(days=31 * self._months)
        dropped = await self._keeper.drop_before(cutoff)
        if dropped:
            logger.info("删掉了保留期之外的调用记录分区：%s", ", ".join(dropped))
