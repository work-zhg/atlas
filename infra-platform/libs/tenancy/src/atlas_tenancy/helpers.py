"""和租户上下文配套的几件小事：请求入口、出站请求头、跨租户巡检、共用设施里的名字。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar
from uuid import UUID

from .context import SystemReason, TenantScope, TenantViolation

__all__ = ["TenantNaming", "TenantSweep", "tenant_headers"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 出站请求头的名字：不带应用名（header-names-no-app-name）
TENANT_HEADER = "X-Tenant-Id"


def tenant_headers(tenant_uuid: UUID | None = None) -> dict[str, str]:
    """出站请求头：把租户写进 X-Tenant-Id。

    不传就取当前租户；系统范围里调用下游必须显式给出目标租户（不知道替谁调，就不该调）。
    """
    tenant = tenant_uuid or TenantScope.current()
    if tenant is None:
        raise TenantViolation(
            TenantViolation.MISSING, "调用下游服务时没有租户上下文，也没有给出目标租户"
        )
    return {TENANT_HEADER: str(tenant)}


class TenantSweep:
    """跨租户后台任务的固定写法：先在系统范围里只取 (待办, 租户)，再逐条进入各自的租户处理。

    最常见的漏洞是在系统范围里一路把业务也做了；固定成两段，处理阶段一定带着具体租户，
    自动过滤照常生效。一条失败只记日志，不挡后面的。
    """

    @staticmethod
    async def each(
        fetch: Callable[[], Awaitable[Iterable[tuple[T, UUID]]]],
        handle: Callable[[T], Awaitable[Any]],
    ) -> tuple[int, int]:
        """→ (成功条数, 失败条数)"""
        with TenantScope.system(SystemReason.SWEEP):
            items = list(await fetch())
        ok = failed = 0
        for item, tenant in items:
            try:
                with TenantScope.enter(tenant):
                    await handle(item)
                ok += 1
            except Exception:
                failed += 1
                logger.exception("巡检处理一条失败：租户 %s · %r", tenant, item)
        return ok, failed


class TenantNaming:
    """名字进入共用设施（对象存储、网关路由、集群标签）时一律带上当前租户。"""

    #: 集群标签的键
    LABEL = "atlas.tenant"

    @staticmethod
    def object_prefix(*parts: str) -> str:
        """对象存储路径：第一层目录是租户。object_prefix("u1", "files") → "<租户>/u1/files"。"""
        tenant = TenantScope.require()
        clean = [p.strip("/") for p in parts if p and p.strip("/")]
        return "/".join([str(tenant), *clean])

    @staticmethod
    def qualified(name: str) -> str:
        """共用命名空间里的名字（如模型网关的路由名）：<租户>:<名字>。"""
        return f"{TenantScope.require()}:{name}"

    @staticmethod
    def labels() -> dict[str, str]:
        return {TenantNaming.LABEL: str(TenantScope.require())}
