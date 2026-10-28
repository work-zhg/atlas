from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter

from ...auth.authz import PrincipalDep
from ...deps import AgentServiceDep, MetaServiceDep
from ...schemas.meta import KnowledgeListResponse, ModelListResponse, ToolListResponse
from ...services.meta import McpSubject

router = APIRouter(tags=["meta"])


@router.get("/models", response_model=ModelListResponse)
async def list_models(
    service: MetaServiceDep,
    principal: PrincipalDep,
    agents: AgentServiceDep,
    agent_id: UUID | None = None,
) -> ModelListResponse:
    """可选的模型。agent_id：编辑已有的智能体；新建时不给。

    经模型管理平台时按这个智能体的客户端 Key 列出（模型管理方案设计 7.2），与权限类型无关；
    没接平台时：model_catalog ⋈ litellm /v1/models 巡检结果。
    """
    if agent_id is None:
        return await service.list_models()
    agent = await agents.get(agent_id, principal=principal)
    return await service.list_models(agent_id=agent.id, agent_name=agent.name)


async def _subject(
    principal: Any, agents: Any, agent_id: UUID | None, auth_subject: str | None
) -> McpSubject:
    if agent_id is not None:
        agent = await agents.get(agent_id, principal=principal)
        acts_as_agent = agent.auth_subject == "agent"
        uid = agent.digital_employee_uid if acts_as_agent else principal.auth_uid
    else:
        acts_as_agent = auth_subject == "agent"
        uid = None if acts_as_agent else principal.auth_uid
    return McpSubject(kind="agent" if acts_as_agent else "user", uid=uid)


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
    return await service.list_tools(
        mcp_subject=await _subject(principal, agents, agent_id, auth_subject)
    )


@router.get("/knowledge-bases", response_model=KnowledgeListResponse)
async def list_knowledge_bases(
    service: MetaServiceDep,
    principal: PrincipalDep,
    agents: AgentServiceDep,
    agent_id: UUID | None = None,
    auth_subject: Literal["user", "agent"] | None = None,
) -> KnowledgeListResponse:
    """编辑器里可选的知识库，按这个 Agent 的权限主体列出（参数同 /tools）：
    以人为权限 = 当前用户能读的；以 Agent 为权限 = 它的数字员工能读的。
    运行时知识库按同一个主体（X-Auth-Uid）判，读不到的库跳过。
    """
    return await service.list_knowledge(await _subject(principal, agents, agent_id, auth_subject))
