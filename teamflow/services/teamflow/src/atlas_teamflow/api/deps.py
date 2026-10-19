"""依赖：数据库会话、请求头身份、操作码鉴权、用户中心 / Atlas 客户端、团队服务。

内网免令牌（内网身份传递 §3）：谁在操作 = 请求头 X-Uid（网关按用户中心登录态写入）。
登录、CSRF 在网关 + 用户中心（/api/v1/auth/verify），这里不再处理。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..atlas import AtlasClient
from ..db.session import get_session
from ..errors import Unauthorized
from ..identity import Principal, TeamLevelCache, resolve
from ..outbound import use_identity
from ..settings import TFSettings, get_settings
from ..teams.projects import ProjectService
from ..teams.service import TeamService
from ..uc import UCClient

__all__ = [
    "AtlasDep",
    "PrincipalDep",
    "ProjectsDep",
    "SessionDep",
    "SettingsDep",
    "TeamsDep",
    "UCDep",
    "require",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _settings(request: Request) -> TFSettings:
    """create_app 传入的配置优先（测试 / 嵌入时与全局配置不同）。"""
    return getattr(request.app.state, "settings", None) or get_settings()


SettingsDep = Annotated[TFSettings, Depends(_settings)]


def _uc(request: Request) -> UCClient:
    return request.app.state.uc  # type: ignore[no-any-return]


def _atlas(request: Request) -> AtlasClient:
    return request.app.state.atlas  # type: ignore[no-any-return]


def _levels(request: Request) -> TeamLevelCache:
    return request.app.state.levels  # type: ignore[no-any-return]


UCDep = Annotated[UCClient, Depends(_uc)]
AtlasDep = Annotated[AtlasClient, Depends(_atlas)]
LevelsDep = Annotated[TeamLevelCache, Depends(_levels)]


async def current_principal(
    request: Request, session: SessionDep, uc: UCDep, settings: SettingsDep
) -> Principal:
    raw = (request.headers.get("x-uid") or "").strip()
    try:
        uid = UUID(raw)
    except ValueError as exc:
        raise Unauthorized("NOT_LOGGED_IN", "请先登录") from exc
    # 出站身份：本请求里调用户中心、Atlas 都以这个人的名义（租户原样透传）
    use_identity(uid, request.headers.get("x-tenant-id") or None)
    return await resolve(session, uc, settings, uid)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(*ops: str):  # type: ignore[no-untyped-def]
    """操作码鉴权（前端隐藏只负责「看不到」）。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        principal.require(*ops)
        return principal

    return Depends(_dep)


def _teams(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    uc: UCDep,
    levels: LevelsDep,
    atlas: AtlasDep,
) -> TeamService:
    return TeamService(session, principal, uc, levels, atlas, repo=request.app.state.repo)


TeamsDep = Annotated[TeamService, Depends(_teams)]


def _projects(teams: TeamsDep) -> ProjectService:
    return ProjectService(teams, teams.repo)


ProjectsDep = Annotated[ProjectService, Depends(_projects)]
