"""运营接口（c4-model/agent/code-ops.html）：运营中心经它管 Atlas，统一挂在 /ops/v1。

通用的部分在运营接口库（atlas_opsapi）：签名校验、幂等、本地的租户状态（TenantGate）、数据库里
租户数据的分批删除、系统参数（应用配置库 · SYSTEM）和系统任务（定时任务库）两组接口。这里只接上
Atlas 特有的：

    lifecycle.py    租户挂起、恢复时 run 和沙箱怎么办；删除时数据库之外还要删什么
    capacity.py     容量契约：总容量、各租户的份额和用量
    runtime_env.py  运行环境契约：沙箱模板

★ 网关不转发 /ops/ 前缀：运营中心在集群内直连。原来按 sandbox:manage、config:manage、job:manage
  判的管理接口都移到这里，Atlas 里不再有平台级的操作码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atlas_opsapi import PREFIX, OpsGuard, OpsRoutes, TenantGate, TenantLifecycleService
from fastapi import APIRouter, FastAPI

from ..db.session import get_sessionmaker
from ..instance import instance_id
from . import capacity, runtime_env
from .lifecycle import AtlasTenantLifecycle, atlas_data_purge

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["mount_ops"]


def mount_ops(app: FastAPI, settings: Settings) -> None:
    """建 TenantGate、OpsGuard 放到 app.state（鉴权、执行器、设备核对都用），挂上 /ops/v1。

    要在 runtime_config、jobs 都放上 app.state 之后调。
    """
    sessions = get_sessionmaker()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    app.state.ops_guard = guard = OpsGuard(key)
    app.state.tenant_gate = gate = TenantGate(sessions)
    lifecycle = TenantLifecycleService(
        sessions, AtlasTenantLifecycle(app.state, settings), gate, atlas_data_purge()
    )
    ops = OpsRoutes(
        guard,
        lifecycle,
        config=app.state.runtime_config.config,
        jobs=app.state.jobs,
        instance=instance_id(),  # 和 run 租约的 owner 一致
    )
    app.include_router(ops.router)
    own = APIRouter(prefix=PREFIX)
    own.include_router(capacity.router)
    own.include_router(runtime_env.router)
    app.include_router(own)
