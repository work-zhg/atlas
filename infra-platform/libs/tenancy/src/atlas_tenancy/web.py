"""请求入口：宿主的身份解析（已向用户中心核对过「这个人在这个租户里」）之后进入租户。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException

from .context import TenantScope

__all__ = ["parse_tenant", "tenant_dependency"]


def parse_tenant(raw: Any) -> UUID | None:
    """请求头里的租户 → UUID；空或格式不对都当没有。"""
    if isinstance(raw, UUID):
        return raw
    if not raw:
        return None
    try:
        return UUID(str(raw).strip())
    except ValueError:
        return None


def tenant_dependency(get_tenant: Callable[..., Any]) -> Callable[..., Any]:
    """FastAPI 依赖工厂：get_tenant 是宿主的依赖，返回已核对过的租户（UUID 或字符串）。

    没有租户的请求一律 401：漏写了 X-Tenant-Id 的内部调用方要尽早暴露，不能悄悄按「没有租户」跑。
    """

    async def _enter(tenant: Any = Depends(get_tenant)) -> UUID:
        value = parse_tenant(tenant)
        if value is None:
            raise HTTPException(status_code=401, detail="缺少租户")
        TenantScope.bind(value)
        return value

    return _enter
