"""配置变了要告诉网关：改账号、模型、线路、Key、授权的事务提交后，按租户递增配置版本号并广播即时失效。

    notify.mark(session, ...)   服务里改配置时标记（区分租户变更、Key 变更与立即驱逐摘要）
    await notify.flush(session) 会话提交之后调用（db/session.get_session 里）

★ 提交之后才发布：网关收到广播或看到新版本号去重新加载时，读到的一定是提交后的数据。
★ 按租户隔离：租户 A 改账号/模型/线路只递增租户 A 的版本号，别的租户不重拉、不重解密。
★ 网关状态没配（本机、测试）就什么都不做；网关按时间兜底重新加载。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope
from sqlalchemy.ext.asyncio import AsyncSession

from .shared.sync import VersionWatch

__all__ = ["flush", "mark", "use_redis"]

logger = logging.getLogger(__name__)

_redis: Any | None = None
_pubsub: Any | None = None
_KEY = "config_changed"


@dataclass
class _PendingChange:
    tenants: set[UUID] = field(default_factory=set)
    tenant_changed: bool = False
    keys_changed: bool = False
    evict_digests: set[str] = field(default_factory=set)


def use_redis(redis: Any | None, *, pubsub: Any | None = None) -> None:
    global _redis, _pubsub
    _redis = redis
    _pubsub = pubsub


def mark(
    session: AsyncSession,
    *,
    tenant: UUID | None = None,
    tenant_changed: bool = True,
    keys_changed: bool = False,
    evict_digests: Iterable[str] = (),
) -> None:
    pending = session.info.get(_KEY)
    if not isinstance(pending, _PendingChange):
        pending = _PendingChange()
        session.info[_KEY] = pending
    resolved_tenant = tenant if tenant is not None else TenantScope.current()
    if resolved_tenant is not None:
        pending.tenants.add(resolved_tenant)
    if tenant_changed:
        pending.tenant_changed = True
    if keys_changed:
        pending.keys_changed = True
    for d in evict_digests:
        if d:
            pending.evict_digests.add(d)


async def flush(session: AsyncSession) -> None:
    pending = session.info.pop(_KEY, None)
    if not isinstance(pending, _PendingChange) or _redis is None:
        return
    try:
        tenants: list[UUID | None] = list(pending.tenants) or [None]
        for idx, tid in enumerate(tenants):
            await VersionWatch.bump(
                _redis,
                tenant_uuid=tid,
                tenant_changed=pending.tenant_changed,
                keys_changed=pending.keys_changed if idx == 0 else False,
                evict_digests=pending.evict_digests if idx == 0 else (),
                pubsub=_pubsub,
            )
    except Exception:
        logger.warning("配置版本号递增或广播失败，网关最晚按时间兜底重新加载", exc_info=True)
