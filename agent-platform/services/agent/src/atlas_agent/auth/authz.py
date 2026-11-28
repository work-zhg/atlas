"""资源授权（权限设计 §7、§8；内网身份传递 §6、§7.3）。

两类判定在这里：
  · 操作码：这类事能不能做（来自用户中心授权快照，按 auth-uid）→ 不够返回 403
  · 归属：会话及其下一切（run、消息、事件、审批、文件、子会话）→ 看不到的返回 404（不泄露对象
          是否存在）。谁看得到按会话的文件主体种类（thread.owner_kind，建会话时定）：
            以人为权限      只有创建人（uid）
            以数字员工为权限 对这个数字员工有使用权（READ）的人，不看创建人 —— 平台只管数据权限，
                            应用要让谁能看、能答，就在业务动作里给谁授权（TeamFlow 发起流程时）

看得到就能答审批、停止、接管浏览器（前端直连平台，接口不带 write）。write = 改这个会话本身：开新
一轮、改名、删、传文件、压缩、在沙箱里执行。业务应用建的会话（thread.source_app，如 TeamFlow）：
同一应用来访时信任它已做过授权，不再要求能看；write 只认这个应用 —— 开新一轮要经它（它记下 run
和流程的对应），不能绕过它的流程状态。

管理员默认也不能读别人的会话（对话是个人数据）。

智能体本身的权限（谁能用、谁能改）是用户中心的数据权限 AtlasAgent，见 agent_access.py。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Run, Thread
from ..errors import Forbidden, NotFound
from .catalog import AGENT_DATA
from .dependency import current_principal
from .principal import LEVEL_RANK, Principal

__all__ = [
    "PrincipalDep",
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


#: 判归属要的会话列（子会话的创建人、来源应用、文件主体都继承自父会话）
_ACCESS_COLUMNS = (Thread.created_by_uuid, Thread.source_app, Thread.owner_kind, Thread.agent_uuid)


async def _can_use(principal: Principal, agent_uuid: UUID) -> bool:
    """对这个智能体（数字员工）有没有使用权：单条授权或「全部智能体」编码级授权 ≥ READ。"""
    if principal.has_all_data(AGENT_DATA):
        return True
    if principal.data_check is None:  # 不经接口的内部调用：没有数据权限可查，不限制
        return True
    level = await principal.data_check(AGENT_DATA, str(agent_uuid))
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK["READ"]


async def _check(row: Any, principal: Principal, *, write: bool, what: str) -> None:
    if row is None:
        raise NotFound(f"{what}不存在")
    trusted_app = row.source_app is not None and principal.app == row.source_app
    if not trusted_app:
        visible = (
            await _can_use(principal, row.agent_uuid)
            if row.owner_kind == "agent"
            else row.created_by_uuid == principal.uid
        )
        if not visible:
            raise NotFound(f"{what}不存在")
    if write and row.source_app is not None and not trusted_app:
        raise Forbidden(f"这个会话来自 {row.source_app}，只能在 {row.source_app} 里继续操作")


async def ensure_thread_owner(
    session: AsyncSession, thread_id: UUID, principal: Principal, *, write: bool = False
) -> None:
    """会话（含子会话）看不看得到、能不能改。子会话的这几列继承自父会话。"""
    row = (
        await session.execute(select(*_ACCESS_COLUMNS).where(Thread.thread_uuid == thread_id))
    ).one_or_none()
    await _check(row, principal, write=write, what="会话")


async def ensure_run_owner(
    session: AsyncSession, run_id: UUID, principal: Principal, *, write: bool = False
) -> None:
    row = (
        await session.execute(
            select(*_ACCESS_COLUMNS)
            .join(Run, Run.thread_uuid == Thread.thread_uuid)
            .where(Run.run_uuid == run_id)
        )
    ).one_or_none()
    await _check(row, principal, write=write, what="运行")
