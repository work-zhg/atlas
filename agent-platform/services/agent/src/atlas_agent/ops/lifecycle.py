"""Atlas 的租户接入（c4-model/agent/code-ops.html · AtlasTenantLifecycle）：创建、删除。

    创建   开通时运营中心给 Atlas 填了份额（running_runs、sandboxes、storage_bytes，null = 不限）
           就记下；一项都没填不建行，用默认份额。能重做：再记一次结果一样
    删除   数据库之外的东西，固定顺序：① 这个租户的全部沙箱和会话 Pod ② 对象存储里这个租户的人的
           全部文件。数据库里的行由运营接口库在这之后删（设备令牌跟着 device 表一起删，就吊销了）——
           行是找到这些东西的线索

★ 不知道租户状态：没有停用、恢复。租户停用、到期后，用户中心对这个租户的人回答无权限，新的 run
  发不起来；在跑、挂起等着的 run 照常（设计接受的代价），删除时一起删。
★ 不读也不返回任何租户的业务内容：只按租户 uuid 动作，报告里只有数字。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_opsapi import Rejected, TenantLifecycle
from atlas_tenancy import PurgeReport
from pydantic import ValidationError
from sqlalchemy import select

from ..browser.component import BrowserComponent
from ..capacity import Quota
from ..db import models as m
from ..db.session import get_sessionmaker
from ..domain.workspace_layout import WorkspaceLayout
from ..providers.filesystem import make_s3_client
from ..redisx import make_redis
from ..sandbox.component import SandboxComponent

if TYPE_CHECKING:
    from ..capacity import CapacityService
    from ..config import Settings

__all__ = ["ATLAS_PURGE_ORDER", "AtlasTenantLifecycle"]

logger = logging.getLogger(__name__)

#: 删数据库里租户数据的顺序：子表在前（没有外键，中途停下也不留指向已删父行的孤儿）
ATLAS_PURGE_ORDER: tuple[type[Any], ...] = (
    m.RunEvent,
    m.RunState,
    m.RunFile,
    m.Approval,
    m.MessageFeedback,
    m.Message,
    m.CliSession,
    m.Run,
    m.ThreadContext,
    m.ThreadMemory,
    m.MemoryBackfill,
    m.Thread,
    m.SkillUsageDaily,
    m.AgentVersion,
    m.Agent,
    m.SandboxExecution,
    m.SandboxBindingRow,
    m.UserSandboxRow,
    m.SandboxAccountRow,
    m.SandboxEvent,
    m.SandboxInstance,
    m.BrowserLogin,
    m.BrowserHostRow,
    m.Device,
    m.MemoryUserSetting,
    m.ServiceModel,
    m.Instruction,
    m.StorageUsageRow,
    m.TenantCapacityRow,
    m.AppUser,
)


#: 开通时给 Atlas 填的份额参数
_QUOTA_FIELDS = ("running_runs", "sandboxes", "storage_bytes")


class AtlasTenantLifecycle(TenantLifecycle):
    """漏了哪张租户表，构造时就报错（新加租户表要排进 ATLAS_PURGE_ORDER）。"""

    purge_order = ATLAS_PURGE_ORDER

    def __init__(self, capacity: CapacityService, settings: Settings) -> None:
        super().__init__(get_sessionmaker(), base=m.Base)
        self._capacity = capacity
        self._settings = settings

    # ------------------------------------------------------------------ 创建

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        if not any(k in params for k in _QUOTA_FIELDS):
            return  # 没填份额：用默认份额
        from .capacity import QuotaIn

        try:
            q = QuotaIn.model_validate({k: params.get(k) for k in _QUOTA_FIELDS})
        except ValidationError as exc:
            raise Rejected(f"份额参数不对：{exc.errors()[0]['msg']}") from exc
        async with self._sessions() as session:
            await self._capacity.set_quota(
                session,
                Quota(q.running_runs, q.sandboxes, q.storage_bytes),
                by="运营中心（开通租户）",
            )
            await session.commit()

    # ------------------------------------------------------------------ 删除

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        report = PurgeReport()
        retired, left = await self._retire_sandboxes(budget)
        if retired:
            report.deleted["sandbox"] = retired
        if left:
            report.remaining = True
            return report
        if self._settings.workspace_configured:
            deleted, more = await self._delete_files(budget - retired)
            if deleted:
                report.deleted["object"] = deleted
            report.remaining = more
        return report

    async def _retire_sandboxes(self, limit: int | None) -> tuple[int, bool]:
        redis = make_redis(self._settings)
        try:
            sandbox = SandboxComponent.build(self._settings, redis)
            # 附加件照常接上（退役前导出浏览器登录态的钩子在它上面）
            BrowserComponent.attach(self._settings, sandbox)
            return await sandbox.provisioning.retire_tenant(limit=limit)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _delete_files(self, budget: int) -> tuple[int, bool]:
        """这个租户的人在对象存储里的全部文件，最多删 budget 个。→ (删了几个, 还有没有剩)。"""
        async with self._sessions() as session:
            users = list(await session.scalars(select(m.AppUser.user_uuid)))
        if budget <= 0:
            return 0, bool(users)
        prefixes = [f"{WorkspaceLayout.of(u, u, '').user_prefix}/" for u in users]
        return await asyncio.to_thread(_delete_prefixes, self._settings, prefixes, budget)


def _delete_prefixes(settings: Settings, prefixes: list[str], budget: int) -> tuple[int, bool]:
    s3 = make_s3_client(settings)
    bucket = settings.oss_bucket
    deleted = 0
    for prefix in prefixes:
        while True:
            page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=min(budget, 1000))
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if not keys:
                break
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            deleted += len(keys)
            budget -= len(keys)
            if budget <= 0:
                return deleted, True
    return deleted, False
