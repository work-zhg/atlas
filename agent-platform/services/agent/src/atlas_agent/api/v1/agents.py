from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from ...auth.authz import PrincipalDep, ensure_agent_editable, require
from ...deps import AgentServiceDep, CurrentUserDep, SessionDep
from ...schemas.agent import (
    AgentCreate,
    AgentDetailOut,
    AgentListOut,
    AgentStatusUpdate,
    AgentUpdate,
    AgentVersionListOut,
)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=AgentListOut)
async def list_agents(
    service: AgentServiceDep,
    principal: PrincipalDep,
    q: Annotated[str | None, Query(description="搜索名称 / 标识 / 描述")] = None,
    agent_status: Annotated[str | None, Query(alias="status")] = None,
    model: Annotated[str | None, Query(description="按模型 id 过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentListOut:
    return await service.list(q=q, status=agent_status, model=model, limit=limit)


@router.post("", response_model=AgentDetailOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    principal: PrincipalDep,
    _: Annotated[object, Depends(require("agent:create"))],
) -> AgentDetailOut:
    """创建时同时写入 version 1，status=draft。"""
    return await service.create(payload, user_id=user_id, principal=principal)


@router.get("/{agent_id}", response_model=AgentDetailOut)
async def get_agent(agent_id: UUID, service: AgentServiceDep) -> AgentDetailOut:
    return await service.get(agent_id)


@router.patch("/{agent_id}", response_model=AgentDetailOut)
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> AgentDetailOut:
    """★ 保存即产生新版本并切换 current_version_id，不做原地更新（§5.4）。"""
    await ensure_agent_editable(session, agent_id, principal)
    return await service.update(agent_id, payload, user_id=user_id, principal=principal)


@router.post("/{agent_id}/status", response_model=AgentDetailOut)
async def set_agent_status(
    agent_id: UUID,
    payload: AgentStatusUpdate,
    service: AgentServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> AgentDetailOut:
    await ensure_agent_editable(session, agent_id, principal)
    return await service.set_status(agent_id, payload.status)


@router.get("/{agent_id}/versions", response_model=AgentVersionListOut)
async def list_agent_versions(agent_id: UUID, service: AgentServiceDep) -> AgentVersionListOut:
    return await service.list_versions(agent_id)


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID, service: AgentServiceDep, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    """归档语义：agent 置 archived、其会话一并归档。

    不是物理删除 —— run.agent_version_id 需要 agent_version 存活才能解释历史会话（§5.4）。
    内置智能体返回 409。
    """
    await ensure_agent_editable(session, agent_id, principal)
    return await service.delete(agent_id)


__all__ = ["Depends", "router"]
