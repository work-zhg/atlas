"""租户设置（c4-model/agent/code-tenant-settings.html · TenantSettingsRoutes）。

    GET   /v1/tenant-settings                       本租户的默认智能体、每人上限，只读的份额和用量
    *     /v1/tenant-settings/configs[/{key}…]      应用配置库的租户设置（scope=TENANT）：列出、
                                                    改值、历史、回滚。看要登录，改要 settings:manage

租户管理员管自己租户的地方。默认智能体、每人上限存在应用配置库的租户设置里，没改过的用系统值
（运营中心经运营接口定）。租户指令在 /v1/instructions/tenant，系统功能用哪个模型在
/v1/service-models，默认智能体也可以经 /v1/agents/default 改（会校验智能体）。

★ 租户取当前请求的租户（解析主体时已进入），不从请求参数里取。
★ 每人上限高于本租户份额时不报错：准入先判租户份额，多出来的那部分用不上（容量组件）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from atlas_appconfig import AppConfig, ConfigRoutes, ConfigScope
from atlas_tenancy import TenantScope
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...auth.dependency import current_principal
from ...auth.principal import Principal
from ...capacity import CapacityService
from ...deps import SessionDep

__all__ = ["SETTINGS_MANAGE", "router", "tenant_config_router"]

#: 租户管理员的操作码：改本租户的设置（租户管理员角色带它）
SETTINGS_MANAGE = "settings:manage"

router = APIRouter(prefix="/tenant-settings", tags=["tenant-settings"])


class QuotaOut(BaseModel):
    """null = 不限。"""

    running_runs: int | None
    sandboxes: int | None
    storage_bytes: int | None
    is_default: bool


class UsageOut(BaseModel):
    running_runs: int
    sandboxes: int
    storage_bytes: int


class PerPersonOut(BaseModel):
    """0 = 不限。"""

    running_runs: int
    sandboxes: int
    storage_bytes: int


class TenantSettingsOut(BaseModel):
    default_agent: UUID | None
    per_person: PerPersonOut
    quota: QuotaOut
    usage: UsageOut
    can_manage: bool


@router.get("", response_model=TenantSettingsOut)
async def view(
    request: Request,
    session: SessionDep,
    principal: Annotated[Principal, Depends(current_principal)],
) -> TenantSettingsOut:
    config = request.app.state.runtime_config
    capacity: CapacityService = request.app.state.capacity
    raw = await config.default_agent()
    try:
        default_agent: UUID | None = UUID(raw)
    except ValueError:
        default_agent = None
    runs, sandboxes, storage = await config.per_person()
    usage = await capacity.usage(session)
    return TenantSettingsOut(
        default_agent=default_agent,
        per_person=PerPersonOut(running_runs=runs, sandboxes=sandboxes, storage_bytes=storage),
        quota=QuotaOut(
            running_runs=usage.quota.running_runs,
            sandboxes=usage.quota.sandboxes,
            storage_bytes=usage.quota.storage_bytes,
            is_default=usage.is_default,
        ),
        usage=UsageOut(
            running_runs=usage.running_runs,
            sandboxes=usage.sandboxes,
            storage_bytes=usage.storage_bytes,
        ),
        can_manage=principal.has(SETTINGS_MANAGE),
    )


async def _settings_user(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> None:
    request.state.settings_user = principal


class SettingsGuard:
    """应用配置库租户设置接口的守卫：看要登录，改要 settings:manage；租户取当前请求的租户。"""

    async def view(self, request: Request) -> str:
        return str(request.state.settings_user.uid)

    async def edit(self, request: Request) -> str:
        principal: Principal = request.state.settings_user
        if not principal.has(SETTINGS_MANAGE):
            raise HTTPException(403, "没有权限执行该操作")
        return str(principal.uid)

    async def tenant(self, request: Request) -> UUID:
        return TenantScope.require()


def tenant_config_router(config: AppConfig) -> APIRouter:
    routes = ConfigRoutes(
        config, SettingsGuard(), scope=ConfigScope.TENANT, prefix="/tenant-settings/configs"
    )
    out = APIRouter(prefix="/v1")
    out.include_router(routes.router, dependencies=[Depends(_settings_user)])
    return out
