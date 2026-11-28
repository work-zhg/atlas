"""接口依赖：数据库会话、调用人（访问控制）、各领域的应用服务。

★ 会话依赖用 scope="function"：写操作在响应发出之前提交（FastAPI 默认在响应之后才执行 yield 后面的
  commit，调用方拿到成功立刻去查可能查不到）。流式响应（SSE）不用它，自己开短会话。
★ 认人时进入调用人的租户（TenantScope.bind），数据按它自动过滤；出站调用以调用人的名义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from atlas_tenancy import TenantScope
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import AccessControl, Action, PeopleDirectory, Principal
from ..agentplatform import AgentPlatform
from ..collab.service import NodeCollab
from ..db.session import get_sessionmaker
from ..inbox.service import InboxService
from ..market.service import MarketService
from ..outbound import use_identity
from ..process.service import ProcessService
from ..projects.service import ProjectService
from ..space.service import FlowSpaces
from ..teams.agents import AgentDirectory
from ..teams.members import TeamMembers
from ..teams.service import TeamService
from ..templates.service import TemplateService
from ..usercenter import UserCenter

__all__ = [
    "AccessDep",
    "PeopleDep",
    "PrincipalDep",
    "Services",
    "ServicesDep",
    "SessionDep",
    "build_services",
    "require",
]


async def _session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(_session, scope="function")]


def _access(request: Request) -> AccessControl:
    return request.app.state.access  # type: ignore[no-any-return]


def _people(request: Request) -> PeopleDirectory:
    return request.app.state.people  # type: ignore[no-any-return]


AccessDep = Annotated[AccessControl, Depends(_access)]
PeopleDep = Annotated[PeopleDirectory, Depends(_people)]


async def current_principal(request: Request, access: AccessDep) -> Principal:
    p = await access.principal(request.headers)
    TenantScope.bind(p.tenant)
    use_identity(p.uid)
    return p


PrincipalDep = Annotated[Principal, Depends(current_principal)]


@dataclass
class Services:
    session: AsyncSession
    teams: TeamService
    templates: TemplateService
    market: MarketService
    projects: ProjectService
    spaces: FlowSpaces
    process: ProcessService
    collab: NodeCollab
    inbox: InboxService


def build_services(session: AsyncSession, uc: UserCenter, atlas: AgentPlatform) -> Services:
    """按领域依赖的方向组装：团队、模板、市场 → 项目 → 流程运行 ⇄ 节点协作（NodeWork 倒置）
    → 待办与通知。"""
    teams = TeamService(session, TeamMembers(uc), AgentDirectory(atlas))
    market = MarketService(session)
    templates = TemplateService(session, market)
    projects = ProjectService(session, teams, templates)
    spaces = FlowSpaces(session, atlas)
    process = ProcessService(session, projects, templates, spaces)
    collab = NodeCollab(session, process, atlas)
    process.work = collab
    return Services(
        session=session,
        teams=teams,
        templates=templates,
        market=market,
        projects=projects,
        spaces=spaces,
        process=process,
        collab=collab,
        inbox=InboxService(session, process),
    )


def _services(request: Request, session: SessionDep, _: PrincipalDep) -> Services:
    return build_services(session, request.app.state.uc, request.app.state.atlas)


ServicesDep = Annotated[Services, Depends(_services)]


async def require(
    access: AccessControl, p: Principal, action: Action, team_uuid: UUID | None = None
) -> None:
    await access.require(p, action, team_uuid)
