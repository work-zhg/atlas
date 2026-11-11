"""智能体接口。

权限（数字员工设计 §8；auth/agent_access.py）：看得到 = AtlasAgent 级别 ≥ 该状态要求；
编辑以人为权限的要 WRITE、以 Agent 为权限的要 OWNER；启用 / 归档、共享、切换权限类型要 OWNER。
看不到的一律 404（不泄露存在），看得到但级别不够 403。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from ...auth.agent_access import edit_level
from ...auth.authz import PrincipalDep, require
from ...deps import AgentServiceDep, CurrentUserDep, ThreadServiceDep
from ...errors import InvalidReference, NotFound
from ...schemas.agent import (
    AgentCreate,
    AgentDetailOut,
    AgentDraftIn,
    AgentGrantCreate,
    AgentGrantPatch,
    AgentIdentityOut,
    AgentListOut,
    AgentPublishIn,
    AgentStatusUpdate,
    AgentUpdate,
    AgentVersionListOut,
    AuthSubjectUpdate,
    DefaultAgentIn,
    DefaultAgentOut,
)
from ...schemas.thread import ThreadCreate, ThreadOut

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=AgentListOut)
async def list_agents(
    service: AgentServiceDep,
    principal: PrincipalDep,
    q: Annotated[str | None, Query(description="搜索名称 / 标识 / 描述")] = None,
    agent_status: Annotated[str | None, Query(alias="status")] = None,
    model: Annotated[str | None, Query(description="按模型 id 过滤")] = None,
    scope: Annotated[
        Literal["mine", "shared"] | None,
        Query(description="mine = 我创建的；shared = 共享给我的；不传 = 全部能看到的"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentListOut:
    return await service.list(
        q=q, status=agent_status, model=model, limit=limit, principal=principal, scope=scope
    )


@router.post("", response_model=AgentDetailOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    principal: PrincipalDep,
    _: Annotated[object, Depends(require("agent:create"))],
) -> AgentDetailOut:
    """创建时同时写入 version 1，status=draft；建数字员工、创建者 = OWNER（数字员工设计 §5.1）。"""
    return await service.create(payload, user_id=user_id, principal=principal)


# ★ /default 必须声明在 /{agent_id} 之前：否则 "default" 先被当成 UUID 解析，直接 422


@router.get("/default", response_model=AgentDetailOut)
async def get_default_agent(
    request: Request, service: AgentServiceDep, principal: PrincipalDep
) -> AgentDetailOut:
    """首页输入框默认用的智能体（平台配置 platform.default_agent）。

    没设、已不是启用状态、或这个人看不到它 → 404：前端退回让用户自己选。
    ★ 默认智能体就是普通智能体：能不能看到照常按共享判，这里不开任何口子。
    """
    raw = await request.app.state.runtime_config.default_agent()
    try:
        agent_id = UUID(raw)
    except ValueError:
        raise NotFound("没有设置默认智能体") from None
    agent = await service.get(agent_id, principal=principal)
    if agent.status != "enabled":
        raise NotFound("默认智能体还没有启用")
    return agent


@router.put("/default", response_model=DefaultAgentOut)
async def set_default_agent(
    payload: DefaultAgentIn,
    request: Request,
    service: AgentServiceDep,
    principal: Annotated[Any, Depends(require("config:manage"))],
) -> DefaultAgentOut:
    """设为默认智能体（agent_id 为空 = 取消默认）。只有平台管理员能改：它影响所有人的首页。"""
    if payload.agent_id is not None:
        agent = await service.agent(payload.agent_id)
        if agent.status != "enabled":
            raise InvalidReference("只有已启用的智能体能设为默认", agent_id=str(payload.agent_id))
    await request.app.state.runtime_config.set_default_agent(
        str(payload.agent_id or ""), by=str(principal.uid)
    )
    return DefaultAgentOut(agent_id=payload.agent_id)


@router.get("/{agent_id}", response_model=AgentDetailOut)
async def get_agent(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> AgentDetailOut:
    return await service.get(agent_id, principal=principal)


@router.patch("/{agent_id}", response_model=AgentDetailOut)
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    principal: PrincipalDep,
) -> AgentDetailOut:
    """★ 保存即产生新版本并切换 current_version_id，不做原地更新（§5.4）。"""
    agent = await service.agent(agent_id)
    await service.access.require(principal, agent, edit_level(agent))
    return await service.update(agent_id, payload, user_id=user_id, principal=principal)


@router.put("/{agent_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def save_draft(
    agent_id: UUID, payload: AgentDraftIn, service: AgentServiceDep, principal: PrincipalDep
) -> Response:
    """编辑器自动保存草稿：不产生版本，「发布」时才写版本。"""
    agent = await service.agent(agent_id)
    await service.access.require(principal, agent, edit_level(agent))
    await service.save_draft(agent_id, payload.spec)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{agent_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> Response:
    """放弃未发布的修改，回到当前版本。"""
    agent = await service.agent(agent_id)
    await service.access.require(principal, agent, edit_level(agent))
    await service.discard_draft(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{agent_id}/publish", response_model=AgentDetailOut)
async def publish_agent(
    agent_id: UUID,
    payload: AgentPublishIn,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    principal: PrincipalDep,
) -> AgentDetailOut:
    """发布：草稿写成新版本，新对话用它。草稿状态的智能体同时启用 —— 启用要所有者。"""
    agent = await service.agent(agent_id)
    await service.access.require(
        principal, agent, "OWNER" if agent.status == "draft" else edit_level(agent)
    )
    return await service.publish(agent_id, note=payload.note, user_id=user_id, principal=principal)


@router.post("/{agent_id}/preview-thread", response_model=ThreadOut, status_code=201)
async def create_preview_thread(
    agent_id: UUID,
    service: AgentServiceDep,
    threads: ThreadServiceDep,
    principal: PrincipalDep,
) -> ThreadOut:
    """调试预览用的会话：不进会话列表，里面的每一轮按草稿（没有草稿就是当前版本）运行。"""
    agent = await service.agent(agent_id)
    await service.access.require(principal, agent, edit_level(agent))
    return await threads.create(
        ThreadCreate(agent_id=agent_id, title="调试预览"), principal=principal, preview=True
    )


@router.post("/{agent_id}/status", response_model=AgentDetailOut)
async def set_agent_status(
    agent_id: UUID, payload: AgentStatusUpdate, service: AgentServiceDep, principal: PrincipalDep
) -> AgentDetailOut:
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    return await service.set_status(agent_id, payload.status)


@router.put("/{agent_id}/auth-subject", response_model=AgentDetailOut)
async def set_auth_subject(
    agent_id: UUID,
    payload: AuthSubjectUpdate,
    service: AgentServiceDep,
    user_id: CurrentUserDep,
    principal: PrincipalDep,
) -> AgentDetailOut:
    """切换权限类型（数字员工设计 §7.1）：只有 OWNER；只影响之后新建的 run。"""
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    return await service.set_auth_subject(agent_id, payload.auth_subject, user_id=user_id)


@router.get("/{agent_id}/identity", response_model=AgentIdentityOut)
async def get_identity(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> AgentIdentityOut:
    """「身份」页签：能用它的人都能看 —— 用户有权知道替他干活的 Agent 能碰什么（§8.2）。"""
    await service.access.require(principal, await service.agent(agent_id))
    return await service.identity(agent_id)


@router.get("/{agent_id}/versions", response_model=AgentVersionListOut)
async def list_agent_versions(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> AgentVersionListOut:
    await service.access.require(principal, await service.agent(agent_id))
    return await service.list_versions(agent_id)


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> dict[str, Any]:
    """归档语义：agent 置 archived、其会话一并归档，数字员工停用。

    不是物理删除 —— run.agent_version_id 需要 agent_version 存活才能解释历史会话（§5.4）。
    """
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    return await service.delete(agent_id)


# ───────────────────────────── 共享（用户中心数据权限 AtlasAgent，数字员工设计 §8.1）


@router.get("/{agent_id}/grants")
async def list_grants(
    agent_id: UUID, service: AgentServiceDep, principal: PrincipalDep
) -> dict[str, Any]:
    """谁能用 / 改这个 Agent。能看到它的人都能看；改要 OWNER（用户中心按 operator 再校验一次）。"""
    level = await service.access.require(principal, await service.agent(agent_id))
    return {"data": await service.grants(agent_id), "my_level": level}


@router.post("/{agent_id}/grants", status_code=status.HTTP_201_CREATED)
async def add_grants(
    agent_id: UUID, body: AgentGrantCreate, service: AgentServiceDep, principal: PrincipalDep
) -> dict[str, bool]:
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    await service.add_grants(agent_id, body, operator=principal.auth_uid)
    return {"ok": True}


@router.patch("/{agent_id}/grants/{acl_id}")
async def update_grant(
    agent_id: UUID,
    acl_id: str,
    body: AgentGrantPatch,
    service: AgentServiceDep,
    principal: PrincipalDep,
) -> dict[str, bool]:
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    await service.update_grant(agent_id, acl_id, body, operator=principal.auth_uid)
    return {"ok": True}


@router.delete("/{agent_id}/grants/{acl_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_grant(
    agent_id: UUID, acl_id: str, service: AgentServiceDep, principal: PrincipalDep
) -> Response:
    await service.access.require(principal, await service.agent(agent_id), "OWNER")
    await service.revoke_grant(agent_id, acl_id, operator=principal.auth_uid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["Depends", "router"]
