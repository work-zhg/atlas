from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter

from ...auth.authz import PrincipalDep
from ...deps import AgentServiceDep, MetaServiceDep
from ...schemas.meta import ModelListResponse, ToolListResponse
from ...services.meta import McpSubject

router = APIRouter(tags=["meta"])


@router.get("/models", response_model=ModelListResponse)
async def list_models(service: MetaServiceDep) -> ModelListResponse:
    """model_catalog ⋈ 网关 /v1/models 巡检结果。"""
    return await service.list_models()


@router.get("/tools", response_model=ToolListResponse)
async def list_tools(
    service: MetaServiceDep,
    principal: PrincipalDep,
    agents: AgentServiceDep,
    agent_id: UUID | None = None,
    auth_subject: Literal["user", "agent"] | None = None,
) -> ToolListResponse:
    """内置工具 + 已接入 MCP 工具。bash 返回 available=false（决策 2）。

    经 MCP 网关时，MCP 工具按这个 Agent 的权限主体列出（MCP 方案设计 §5.4、§9.2）：
    以人为权限 = 当前用户自己的权限；以 Agent 为权限 = 它的数字员工的权限。
    agent_id：编辑已有的 Agent；新建时不给，按 auth_subject（缺省 = 以人为权限）。
    """
    if agent_id is not None:
        agent = await agents.get(agent_id, principal=principal)
        acts_as_agent = agent.auth_subject == "agent"
        uid = agent.digital_employee_uid if acts_as_agent else principal.auth_uid
    else:
        acts_as_agent = auth_subject == "agent"
        uid = None if acts_as_agent else principal.auth_uid
    return await service.list_tools(
        mcp_subject=McpSubject(kind="agent" if acts_as_agent else "user", uid=uid)
    )
