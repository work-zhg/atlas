"""定期对账（方案 6.3）：逐把 Key 比较平台算出的有效模型、停用状态和 LiteLLM 里的，
不一致以平台为准。

对账能补上的：
    · 同步失败（LiteLLM 当时不可达）
    · 用户中心那边的变化：员工调部门、被停用 —— 用户中心没有变更通知，只能定期重算
    · 有人在 LiteLLM 管理界面里改了平台管的 Key
LiteLLM 里找不到的 Key 只记 sync_error，不自动重建（要人看一眼：是不是被手工删了）。

★ 多副本各自跑：同步是幂等的，重复做只是多问几次 LiteLLM。
★ 定时是定时任务库 atlas_jobs 的实例任务（Reconcile）：循环、出错隔离、停机交给库，运行情况在
  /api/v1/jobs 看。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from atlas_jobs import Job, JobContext, NoParams
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import use_outbound
from .db.models import GwApiKey
from .keys import KeyService
from .litellm_api import LiteLLM
from .perms import Permissions, SubjectsCache
from .registry import DeploymentsCache, ModelRegistry

logger = logging.getLogger(__name__)

__all__ = ["Reconcile", "ReconcileReport", "reconcile_once"]


@dataclass
class ReconcileReport:
    checked: int = 0
    changed: int = 0
    failed: int = 0


async def reconcile_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    litellm: LiteLLM,
    deployments: DeploymentsCache,
    perms: Permissions,
    subjects: SubjectsCache,
) -> ReconcileReport:
    report = ReconcileReport()
    async with sessionmaker() as session:
        keys = KeyService(
            session, litellm, ModelRegistry(session, litellm, deployments, perms), subjects
        )
        deployments.forget()
        rows = list(await session.scalars(select(GwApiKey).where(GwApiKey.status != "pending")))
        for key in rows:
            report.checked += 1
            # 没有入站请求：用户 Key 按持有人调用户中心（用户中心据此确定租户）
            use_outbound({"X-Uid": str(key.user_uuid)} if key.user_uuid else None)
            if await keys.sync(key, verify=True, fresh=True):
                report.changed += 1
            if key.sync_error:
                report.failed += 1
        use_outbound(None)
        await session.commit()
    if report.changed or report.failed:
        logger.info(
            "对账：检查 %d 把 Key，修正 %d 把，%d 把同步失败",
            report.checked,
            report.changed,
            report.failed,
        )
    return report


class Reconcile(Job):
    type = "modelgateway.reconcile"
    description = "LiteLLM 对账"

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        litellm: LiteLLM,
        deployments: DeploymentsCache,
        perms: Permissions,
        subjects: SubjectsCache,
    ) -> None:
        self._args = (sessionmaker, litellm, deployments, perms, subjects)

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await reconcile_once(*self._args)
