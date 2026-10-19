"""资源授权（权限设计 §7、§8；内网身份传递 §6、§7.3）。

两类判定在这里：
  · 操作码：这类事能不能做（来自用户中心授权快照，按 auth-uid）→ 不够返回 403
  · 归属：会话及其下一切（run、消息、事件、审批、文件、子会话）归「根会话的所有者」（uid）
          → 不属于你返回 404（不泄露对象是否存在）

业务应用建的会话（thread.source_app，如 TeamFlow）：同一应用来访时信任它已做过授权（例如
TeamFlow 的执行角色校验），不再要求 uid 就是所有者 —— 团队里其他执行人也能经 TeamFlow 看到。
从别处（网关）来的只能看、不能写：续聊要回到 TeamFlow，不能绕过它的流程状态。

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
from .catalog import AGENT_DATA
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


def _check_owner(
    owner: UUID | None, source_app: str | None, principal: Principal, *, write: bool, what: str
) -> None:
    trusted_app = source_app is not None and principal.app == source_app
    if owner is None or (owner != principal.uid and not trusted_app):
        raise NotFound(f"{what}不存在")
    if write and source_app is not None and not trusted_app:
        raise Forbidden(f"这个会话来自 {source_app}，只能在 {source_app} 里继续操作")


async def ensure_thread_owner(
    session: AsyncSession, thread_id: UUID, principal: Principal, *, write: bool = False
) -> None:
    """会话（含子会话）归根会话所有者。子会话的 created_by / source_app 继承自父会话。"""
    row = (
        await session.execute(
            select(Thread.created_by, Thread.source_app).where(Thread.id == thread_id)
        )
    ).one_or_none()
    owner, source_app = (row.created_by, row.source_app) if row else (None, None)
    _check_owner(owner, source_app, principal, write=write, what="会话")


async def ensure_run_owner(
    session: AsyncSession, run_id: UUID, principal: Principal, *, write: bool = False
) -> None:
    row = (
        await session.execute(
            select(Thread.created_by, Thread.source_app)
            .join(Run, Run.thread_id == Thread.id)
            .where(Run.id == run_id)
        )
    ).one_or_none()
    owner, source_app = (row.created_by, row.source_app) if row else (None, None)
    _check_owner(owner, source_app, principal, write=write, what="运行")


async def ensure_agent_editable(
    session: AsyncSession, agent_id: UUID, principal: Principal
) -> None:
    """编辑 / 改状态 / 归档：创建者，或对全部智能体有 Owner；内置智能体只有后者（权限设计 §8）。

    「全部智能体 · Owner」是用户中心的数据编码级授权（数据权限领域），不是角色上的操作码。
    P2 单个智能体也改为用户中心数据权限（WRITE / OWNER）。
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFound(f"智能体 {agent_id} 不存在", agent_id=str(agent_id))
    if principal.has_all_data(AGENT_DATA, "OWNER"):
        return
    if agent.is_builtin:
        raise Forbidden("内置智能体只有管理员可以修改")
    if agent.created_by != principal.uid:
        raise Forbidden("只有智能体的创建者或管理员可以修改")


def can_use_agent(principal: Principal, agent: Agent) -> bool:
    """P1：用户可用全部智能体。P2 换成用户中心数据权限（AtlasAgent READ）。"""
    return True
