"""依赖：数据库会话、登录态（含 CSRF）、操作码鉴权、用户中心 / Atlas 客户端、团队服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..atlas import AtlasClient
from ..db.session import get_session
from ..errors import Forbidden
from ..identity import Principal, TeamLevelCache, resolve
from ..settings import TFSettings, get_settings
from ..teams.projects import ProjectService
from ..teams.service import TeamService
from ..uc import UCClient

__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "AtlasDep",
    "PrincipalDep",
    "ProjectsDep",
    "SessionDep",
    "SettingsDep",
    "TeamsDep",
    "UCDep",
    "require",
]

SESSION_COOKIE = "tf_session"
CSRF_COOKIE = "tf_csrf"
CSRF_HEADER = "x-tf-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[TFSettings, Depends(get_settings)]


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
    p = await resolve(session, uc, settings, request.cookies.get(SESSION_COOKIE))
    if request.method not in _SAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie or cookie != request.headers.get(CSRF_HEADER):
            raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    return p


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(*ops: str):  # type: ignore[no-untyped-def]
    """操作码鉴权（前端隐藏只负责「看不到」）。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        principal.require(*ops)
        return principal

    return Depends(_dep)


def _teams(
    session: SessionDep, principal: PrincipalDep, uc: UCDep, levels: LevelsDep, atlas: AtlasDep
) -> TeamService:
    return TeamService(session, principal, uc, levels, atlas)


TeamsDep = Annotated[TeamService, Depends(_teams)]


def _projects(teams: TeamsDep) -> ProjectService:
    return ProjectService(teams)


ProjectsDep = Annotated[ProjectService, Depends(_projects)]
