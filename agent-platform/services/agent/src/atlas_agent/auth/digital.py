"""Agent ↔ 数字员工、Agent 在用户中心的登记（数字员工设计 §5、§8.1）。

  建 Agent  → 用户中心建数字员工（幂等，external_ref = agent id）+ 登记数据对象、创建者 = OWNER
  改名      → 数字员工、数据对象同步改名
  归档      → 停用数字员工（之后它不再能读任何东西，含历史会话里继续对话）
  恢复      → 启用，授权原样还在

★ 两种权限类型都建数字员工：以人为权限时它不参与判权，但审计、业务系统里的成员 / 处理人
  要用它，以后切到「以 Agent 为权限」也不用补建 —— 只有一条创建路径。
★ 没有用户中心客户端（不经接口的内部调用）时什么都不做；这时不能选「以 Agent 为权限」。
★ 失败直接抛（503）：调用方的事务随之回滚。数字员工建成而 Atlas 没提交的，重试时
  external_ref 相同、拿回同一个 uid，不会重复。
"""

from __future__ import annotations

import logging
from uuid import UUID

from ..db.models import Agent
from ..errors import Conflict
from .catalog import AGENT_DATA
from .uc import UCClient

__all__ = ["AgentIdentity", "digital_name"]

log = logging.getLogger("atlas_agent.auth")


def digital_name(agent_name: str) -> str:
    """数字员工的姓名：🤖 + Agent 名（用户中心姓名列 32 字，超长由它截断）。"""
    return f"🤖 {agent_name}"


class AgentIdentity:
    def __init__(self, uc: UCClient | None) -> None:
        self.uc = uc

    @property
    def available(self) -> bool:
        return self.uc is not None

    def require(self) -> UCClient:
        if self.uc is None:
            raise Conflict("未接入用户中心，没有数字员工，不能「以 Agent 为权限」")
        return self.uc

    async def ensure_digital_employee(self, agent: Agent, owner: UUID) -> UUID | None:
        """没有就建（幂等）。返回数字员工 uid；没有用户中心客户端时返回 None。"""
        if agent.digital_employee_uid is not None or self.uc is None:
            return agent.digital_employee_uid
        de = await self.uc.create_digital_employee(
            external_ref=str(agent.id),
            name=digital_name(agent.name),
            owner_id=owner,
            description=(agent.description or "")[:200] or None,
        )
        agent.digital_employee_uid = UUID(str(de["id"]))
        log.info("agent=%s 数字员工=%s（%s）", agent.id, de["id"], de.get("account"))
        return agent.digital_employee_uid

    async def register(self, agent: Agent, owner: UUID) -> None:
        """登记数据对象 AtlasAgent：创建者 = OWNER（首次写入不需要 operator）。"""
        if self.uc is None:
            return
        await self.uc.data_grant(
            AGENT_DATA,
            str(agent.id),
            [{"permission": "OWNER", "subject_type": "USER", "subject_id": str(owner)}],
            data_name=agent.name,
        )

    async def renamed(self, agent: Agent) -> None:
        if self.uc is None:
            return
        await self.uc.data_rename(AGENT_DATA, str(agent.id), agent.name)
        if agent.digital_employee_uid is not None:
            await self.uc.update_digital_employee(
                agent.digital_employee_uid, name=digital_name(agent.name)
            )

    async def status_changed(self, agent: Agent, *, before: str) -> None:
        """归档 → 停用数字员工；从归档恢复 → 启用。草稿 ↔ 启用不动（草稿也要能试跑）。"""
        if self.uc is None or agent.digital_employee_uid is None or before == agent.status:
            return
        if agent.status == "archived":
            await self.uc.set_digital_employee_enabled(agent.digital_employee_uid, False)
        elif before == "archived":
            await self.uc.set_digital_employee_enabled(agent.digital_employee_uid, True)
