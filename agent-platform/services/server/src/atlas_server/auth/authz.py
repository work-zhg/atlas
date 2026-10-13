"""资源授权（权限设计 §7、§8）。

两类判定在这里：
  · 操作码：这类事能不能做（来自用户中心授权快照 / 服务账号配置）→ 不够返回 403
  · 归属：会话及其下一切（run、消息、事件、审批、文件、子会话）归「根会话的所有者」
          → 不属于你返回 404（不泄露对象是否存在）

管理员默认也不能读别人的会话（对话是个人数据）。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Agent, Run, Thread
from ..errors import Forbidden, NotFound
from .dependency import current_principal
from .principal import Principal

__all__ = [
    "PrincipalDep",
    "can_use_agent",
    "ensure_agent_editable",
    "ensure_run_owner",
    "ensure_thread_owner",
    "require",
]

PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(*ops: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """操作码鉴权依赖：缺任一个 → 403（前端隐藏只负责「看不到」）。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        missing = [op for op in ops if not principal.has(op)]
        if missing:
            raise Forbidden("没有权限执行该操作", missing=",".join(missing))
        return principal

    return _dep


async def ensure_thread_owner(session: AsyncSession, thread_id: UUID, principal: Principal) -> None:
    """会话（含子会话）归根会话所有者。子会话的 created_by 继承自父会话，直接比较即可。"""
    owner = await session.scalar(select(Thread.created_by).where(Thread.id == thread_id))
    if owner is None or owner != principal.owner_id:
        raise NotFound("会话不存在", thread_id=str(thread_id))


async def ensure_run_owner(session: AsyncSession, run_id: UUID, principal: Principal) -> None:
    owner = await session.scalar(
        select(Thread.created_by).join(Run, Run.thread_id == Thread.id).where(Run.id == run_id)
    )
    if owner is None or owner != principal.owner_id:
        raise NotFound("运行不存在", run_id=str(run_id))


async def ensure_agent_editable(
    session: AsyncSession, agent_id: UUID, principal: Principal
) -> None:
    """编辑 / 改状态 / 归档：创建者，或有「管理全部智能体」；内置智能体只有后者（权限设计 §8）。

    P2 改为用户中心数据权限（WRITE / OWNER）。
    """
    agent = await session.get(Agent, agent_id)
    if agent is None or (principal.kind == "service" and not can_use_agent(principal, agent)):
        raise NotFound(f"智能体 {agent_id} 不存在", agent_id=str(agent_id))
    if principal.has("agent:manage_all"):
        return
    if agent.is_builtin:
        raise Forbidden("内置智能体只有管理员可以修改")
    # ★ 创建者规则对服务账号同样适用：有 agent:create 的服务账号建的智能体，它自己要能启用、修改
    #   （否则建完就是一个它既用不了也改不了的草稿）。owner_id 对服务账号就是服务账号 id。
    if agent.created_by != principal.owner_id:
        raise Forbidden("只有智能体的创建者或管理员可以修改")


def can_use_agent(principal: Principal, agent: Agent) -> bool:
    """P1：服务账号按其可用智能体范围；用户可用全部已启用的智能体。
    P2 换成用户中心数据权限（AtlasAgent READ）。"""
    if principal.kind == "service":
        # 自己建的智能体（含草稿）总是可用 —— 见 ensure_agent_editable 的创建者规则
        if getattr(agent, "created_by", None) == principal.owner_id:
            return True
        if principal.agent_scope == "list":
            return agent.id in principal.agent_ids
        return agent.status == "enabled"
    return True
