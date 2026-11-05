"""沙箱组件的装配：按配置把基座、通用供给和三个业务领域装在一起（组合根用）。

    lifecycle_servers   lifecycle server 实例：sbx、cli，部署了评测档时还有 bench
                        （名字 = 模板绑定的 server）
    SandboxComponent    一组装好的对象：runtime、exec、egress、provisioning、native、pods、ledger

★ 构造不做 IO：哪里有 Redis 客户端就在哪里装一份（一段 run、一次维护、一个请求）。进程内的缓存
  （供给的 by_biz、execd 的地址）跟着各自的生命周期走。
★ 通用供给在这里登记两种业务类型（NATIVE、CLI_AGENT）：供给不认识它们，业务领域的服务也不直接调
  自己的种类。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from ..db.session import get_sessionmaker
from .base.execution.execd import ExecdChannel
from .base.execution.ledger import ActivityLedger
from .base.lifecycle.runtime import OpenSandboxRuntime
from .base.lifecycle.server import LifecycleServer
from .base.network.egress import OpenSandboxEgress
from .exec import ExecutionLedger
from .native import ImagePolicy, NativeSandboxKind, NativeSandboxService
from .provisioning.model import QuotaPolicy
from .provisioning.service import SandboxProvisioning
from .session_pod import CliPodKind, SessionPods

if TYPE_CHECKING:
    from datetime import datetime

    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings

__all__ = ["SandboxComponent", "lifecycle_servers"]


def lifecycle_servers(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, LifecycleServer]:
    pairs = {
        "sbx": (settings.opensandbox_sbx_url, settings.opensandbox_sbx_api_key),
        "cli": (settings.opensandbox_cli_url, settings.opensandbox_cli_api_key),
    }
    if settings.opensandbox_bench_url is not None:
        pairs["bench"] = (settings.opensandbox_bench_url, settings.opensandbox_bench_api_key)
    return {
        name: LifecycleServer(
            name, str(url), key, timeout_s=settings.opensandbox_timeout_s, transport=transport
        )
        for name, (url, key) in pairs.items()
    }


@dataclass
class SandboxComponent:
    runtime: OpenSandboxRuntime
    exec: ExecdChannel
    egress: OpenSandboxEgress
    provisioning: SandboxProvisioning
    native: NativeSandboxService
    pods: SessionPods
    ledger: ExecutionLedger

    @classmethod
    def build(
        cls,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        files_transport: httpx.BaseTransport | None = None,
    ) -> SandboxComponent:
        sessions = sessions or get_sessionmaker()
        runtime = OpenSandboxRuntime(
            lifecycle_servers(settings, transport=transport),
            sessions,
            redis,
            launch_wait_s=settings.opensandbox_timeout_s,
            settle_timeout_s=settings.opensandbox_timeout_s,
        )
        exec = ExecdChannel(
            runtime,
            ActivityLedger(redis),
            usage_scope=settings.sandbox_usage_scope,
            transport=transport,
            files_transport=files_transport,
        )
        egress = OpenSandboxEgress(
            runtime, ready_timeout_s=settings.cli_ready_timeout_s, transport=transport
        )
        provisioning = SandboxProvisioning(
            runtime,
            sessions,
            quota=QuotaPolicy(settings.sandbox_quota_per_user),
            cache_s=settings.sandbox_binding_cache_s,
        )
        images = ImagePolicy(
            settings.sandbox_default_image,
            {
                "strict": list(settings.sandbox_image_allow_prefixes),
                "bench": list(settings.sandbox_bench_image_allow_prefixes),
            },
            bench_available=settings.opensandbox_bench_url is not None,
        )
        provisioning.register(NativeSandboxKind(settings, exec, images))
        provisioning.register(CliPodKind(settings, runtime, egress, transport=transport))

        async def open_runs(thread_uuid: UUID) -> tuple[bool, datetime | None]:
            from ..repositories.run import RunRepository

            async with sessions() as session:
                runs = RunRepository(session)
                return await runs.has_open_run(thread_uuid), await runs.last_ended_at(thread_uuid)

        return cls(
            runtime=runtime,
            exec=exec,
            egress=egress,
            provisioning=provisioning,
            native=NativeSandboxService(provisioning, exec, idle_ttl_s=settings.sandbox_idle_ttl_s),
            pods=SessionPods(provisioning, runtime, settings, runs=open_runs),
            ledger=ExecutionLedger(redis),
        )
