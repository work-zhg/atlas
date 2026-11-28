"""运营接口（/ops/v1）：租户的创建、删除。只给运营中心（先验签名），路由由 atlas_opsapi 给出。

    PUT    /ops/v1/tenants/{租户}   创建：不用准备什么
    DELETE /ops/v1/tenants/{租户}   删除：先向 Agent 平台释放本租户的流程空间，再分批删表

★ TeamFlow 不知道租户状态：没有停用、恢复。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_opsapi import OpsGuard, OpsRoutes, TenantLifecycle
from atlas_tenancy import PurgeReport
from fastapi import FastAPI

from ..audit import AuditEvent
from ..collab.models import Instruction, NodeSession
from ..db.base import Base
from ..db.session import get_sessionmaker
from ..inbox.models import ChannelBinding, Notification, TeamChannel
from ..process.models import Process, ProcessEvent, ProcessNode, ReviewRound, Submission, Vote
from ..projects.models import Project, ProjectRole, ProjectTemplate
from ..space.models import FlowSpace
from ..space.service import FlowSpaces
from ..teams.models import Team, TeamAgent
from ..templates.models import FlowTemplate, TemplateDraft, TemplateVersion

__all__ = ["TeamFlowTenantLifecycle", "mount_ops"]


class TeamFlowTenantLifecycle(TenantLifecycle):
    purge_order = (
        Instruction,
        NodeSession,
        Vote,
        ReviewRound,
        Submission,
        ProcessEvent,
        ProcessNode,
        Process,
        FlowSpace,
        ProjectRole,
        ProjectTemplate,
        Project,
        TemplateVersion,
        TemplateDraft,
        FlowTemplate,
        TeamAgent,
        Team,
        Notification,
        TeamChannel,
        ChannelBinding,
        AuditEvent,
    )

    def __init__(self, app: FastAPI) -> None:
        super().__init__(get_sessionmaker(), base=Base)
        self._app = app

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        return None

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        """在这个租户的上下文里：逐个释放流程空间（库里的空间行是找到它们的线索，所以先删外部）。"""
        report = PurgeReport()
        async with self._sessions() as session:
            released = await FlowSpaces(session, self._app.state.atlas).release_tenant(budget)
            await session.commit()
        if released:
            report.deleted["flow_space"] = released
            report.remaining = released >= budget
        return report


def mount_ops(app: FastAPI, ops_api_key: str | None) -> None:
    app.state.ops_guard = guard = OpsGuard(ops_api_key)
    OpsRoutes.mount(app, guard, TeamFlowTenantLifecycle(app))
