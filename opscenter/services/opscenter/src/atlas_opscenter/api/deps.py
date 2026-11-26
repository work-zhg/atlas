"""依赖：数据库会话、运营人员（网关写的身份头 + 用户中心的授权）、按模块判级别。

★ 只有一条判断规则（c4-model/opscenter/code-access.html · AccessPolicy）：这个人在这个模块的级别
  ≥ 这个动作要的级别。看要只读，改要读写，不可恢复的要 Owner。跨模块的只有查运营审计（ops:audit）。
★ 登录、CSRF 都在 API 网关和用户中心：经网关的 auth 路由进来的请求已经验过访问 JWT、校过 CSRF，
  这里只认网关写的 X-Uid、X-Tenant-Id（外部带来的早被网关清掉）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import AUDIT_OP, Level, ModuleCatalog, Principal, PrincipalResolver
from ..db.session import get_session
from ..gateway import OpsClient
from ..settings import OpsSettings, get_settings

__all__ = [
    "AuditorDep",
    "CatalogDep",
    "ClientDep",
    "PrincipalDep",
    "SessionDep",
    "SettingsDep",
    "TenantOwnerDep",
    "TenantReadDep",
    "TenantWriteDep",
]

# ★ scope="function"：请求事务在响应发出之前提交（同用户中心）
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[OpsSettings, Depends(get_settings)]


def _client(request: Request) -> OpsClient:
    return request.app.state.ops_client  # type: ignore[no-any-return]


def _catalog(request: Request) -> ModuleCatalog:
    return request.app.state.modules  # type: ignore[no-any-return]


ClientDep = Annotated[OpsClient, Depends(_client)]
CatalogDep = Annotated[ModuleCatalog, Depends(_catalog)]


async def current_principal(request: Request) -> Principal:
    resolver: PrincipalResolver = request.app.state.resolver
    return await resolver.resolve(request.headers)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _tenant_module(need: Level):  # type: ignore[no-untyped-def]
    async def dep(p: PrincipalDep, catalog: CatalogDep) -> Principal:
        p.require(catalog.tenant(), need)
        return p

    return Depends(dep)


#: 租户模块：看租户、操作进度
TenantReadDep = Annotated[Principal, _tenant_module(Level.READ)]
#: 租户模块：开通、停用、恢复、补建、给租户开通应用、立即重试
TenantWriteDep = Annotated[Principal, _tenant_module(Level.WRITE)]
#: 租户模块：关闭租户（删数据）、补发首个管理员的临时密码
TenantOwnerDep = Annotated[Principal, _tenant_module(Level.OWNER)]


async def current_auditor(p: PrincipalDep) -> Principal:
    p.require_op(AUDIT_OP)
    return p


AuditorDep = Annotated[Principal, Depends(current_auditor)]
