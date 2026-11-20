"""Atlas 对租户生命周期的实现（c4-model/agent/code-ops.html · AtlasTenantLifecycle）。

运营接口库（atlas_opsapi）先改本地的租户状态（TenantGate 立刻挡住新的工作），再在这个租户的
上下文里调这里的钩子。都要幂等：上一次可能做了一半就超时，运营中心会用同一个幂等键重做。

    挂起   本实例上这个租户在跑的 run 交出去（别的实例续租时自己查到、自己交）；排队的不动，
           谁也接不了（try_take 先问 TenantGate）。这个租户的全部沙箱和会话 Pod 退役。
           设备：在线的断开；之后重连也连不上（设备网关核对令牌时回 403）
    恢复   什么都不用做：交出去的、排队的、挂起等着的 run 由兜底任务照常接；沙箱用到时再建
    删除   数据库之外的东西，固定顺序：① 剩下的沙箱和会话 Pod（挂起时已退役，这里兜底）
           ② 对象存储里这个租户的人的全部文件。数据库里的行由运营接口库在这之后删（设备令牌
           跟着 device 表一起删，就吊销了）—— 行是找到这些东西的线索

★ 不读也不返回任何租户的业务内容：只按租户 uuid 动作，报告里只有数字。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_tenancy import PurgeReport, TenantDataPurge
from sqlalchemy import select

from ..browser.component import BrowserComponent
from ..db import models as m
from ..db.session import get_sessionmaker
from ..device.gateway import DeviceGateway
from ..domain.workspace_layout import WorkspaceLayout
from ..providers.filesystem import make_s3_client
from ..redisx import make_redis
from ..sandbox.component import SandboxComponent

if TYPE_CHECKING:
    from ..config import Settings
    from ..executor.scheduling import RunWorker

__all__ = ["ATLAS_PURGE_ORDER", "AtlasTenantLifecycle", "atlas_data_purge"]

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


def atlas_data_purge() -> TenantDataPurge:
    """漏了哪张租户表，构造时就报错（新加租户表要排进上面的顺序）。"""
    return TenantDataPurge(ATLAS_PURGE_ORDER, base=m.Base)


class AtlasTenantLifecycle:
    """实现 atlas_opsapi.TenantLifecycle。执行器在 lifespan 里才建好，所以按需从 app.state 取。"""

    def __init__(self, state: Any, settings: Settings) -> None:
        self._state = state
        self._settings = settings

    # ------------------------------------------------------------------ 挂起、恢复

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        worker: RunWorker | None = getattr(self._state, "executor", None)
        if worker is not None and (n := worker.pause_tenant(tenant_uuid)):
            logger.info("租户 %s 停用：交出本实例上它的 %d 个 run", tenant_uuid, n)
        retired, left = await self._retire_sandboxes(None)
        logger.info(
            "租户 %s 停用：退役 %d 个沙箱%s", tenant_uuid, retired, "（有失败）" if left else ""
        )
        if n := await self._disconnect_devices():
            logger.info("租户 %s 停用：断开 %d 台在线设备", tenant_uuid, n)

    async def on_resume(self, tenant_uuid: UUID) -> None:
        logger.info("租户 %s 恢复：run 由兜底任务照常接，沙箱用到时再建", tenant_uuid)

    # ------------------------------------------------------------------ 删除

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        report = PurgeReport()
        retired, left = await self._retire_sandboxes(limit)
        if retired:
            report.deleted["sandbox"] = retired
        if left:
            report.remaining = True
            return report
        if self._settings.workspace_configured:
            deleted, more = await self._delete_files(limit - retired)
            if deleted:
                report.deleted["object"] = deleted
            report.remaining = more
        return report

    async def _retire_sandboxes(self, limit: int | None) -> tuple[int, bool]:
        redis = make_redis(self._settings)
        try:
            sandbox = SandboxComponent.build(self._settings, redis)
            # 退役前导出浏览器登录态（附加件）：停用后恢复，登录还在
            BrowserComponent.attach(self._settings, sandbox)
            return await sandbox.provisioning.retire_tenant(limit=limit)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()

    async def _disconnect_devices(self) -> int:
        """断开这个租户还在线的本机设备。不在线的跳过；失败只记日志（重连时设备网关会挡住）。"""
        gateway = DeviceGateway(self._settings)
        if not gateway.configured:
            return 0
        async with get_sessionmaker()() as session:
            devices = list(
                await session.scalars(
                    select(m.Device.device_uuid).where(m.Device.status != "revoked")
                )
            )
        done = 0
        for device in devices:
            try:
                if await gateway.presence(str(device)) is None:
                    continue
                await gateway.disconnect(str(device), "企业账号已停用")
                done += 1
            except Exception:
                logger.warning("断开设备 %s 失败", device, exc_info=True)
        return done

    async def _delete_files(self, budget: int) -> tuple[int, bool]:
        """这个租户的人在对象存储里的全部文件，最多删 budget 个。→ (删了几个, 还有没有剩)。"""
        async with get_sessionmaker()() as session:
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
