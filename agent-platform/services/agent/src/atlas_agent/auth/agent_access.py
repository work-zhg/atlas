"""人 → Agent 的数据权限（数字员工设计 §8.1；权限设计 §7.2）：用户中心数据编码 AtlasAgent。

级别与能力，叠加 Agent 的状态：
  READ   看到已启用的、用它建会话 / 发消息          ← 两种权限类型唯一按人判的闸门
  WRITE  看草稿；编辑以人为权限的 Agent（改了也超不出使用者自己的权限）
  OWNER  看已归档的；启用 / 归档、共享、切换权限类型；编辑以 Agent 为权限的 Agent
         （谁能改它的提示词和工具，谁就间接握有数字员工的全部权限，§8.2）
有效级别 = max(这个 Agent 的授权, 「全部智能体」编码级授权)，由用户中心算。
没有「内置智能体」：每个 Agent 都由人创建、按共享决定谁能用（迁移 0024 删掉了这个概念）。

★ 按 auth-uid 判（按谁的权限判）。人直接操作时它就是 uid。
★ 不经接口的内部调用（脚本、单测直接构造服务）没有用户中心客户端：不限制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, or_, true

from ..db.models import Agent
from ..errors import Forbidden, NotFound
from .catalog import AGENT_DATA
from .principal import LEVEL_RANK, Principal
from .uc import UCClient

__all__ = ["STATUS_MIN", "AgentAccess", "Visibility", "edit_level"]

#: 看得到这个 Agent 至少要的级别（按状态）
STATUS_MIN = {"enabled": "READ", "draft": "WRITE", "archived": "OWNER"}


def _max(*levels: str) -> str:
    return max(levels, key=lambda lv: LEVEL_RANK.get(lv, 0))


def edit_level(agent: Agent) -> str:
    """编辑（保存新版本）要的级别：以 Agent 为权限的收紧到 OWNER。"""
    return "OWNER" if agent.acts_as_itself else "WRITE"


@dataclass(frozen=True)
class Visibility:
    """列表的可见范围：单条授权 + 编码级授权。all_ = 不用过滤（全部智能体 · Owner / dev）。"""

    levels: dict[UUID, str]
    type_level: str = "NONE"
    all_: bool = False

    def condition(self) -> Any:
        """SQL 条件：级别 ≥ 该状态的要求。"""
        if self.all_:
            return true()
        conds = []
        for status, need in STATUS_MIN.items():
            if LEVEL_RANK[self.type_level] >= LEVEL_RANK[need]:
                conds.append(Agent.status == status)
                continue
            ids = [i for i, lv in self.levels.items() if LEVEL_RANK[lv] >= LEVEL_RANK[need]]
            if ids:
                conds.append(and_(Agent.status == status, Agent.id.in_(ids)))
        return or_(*conds) if conds else false()


class AgentAccess:
    def __init__(self, uc: UCClient | None) -> None:
        self.uc = uc

    async def level(self, principal: Principal, agent: Agent) -> str:
        if self.uc is None:  # 不经接口的内部调用：没有数据权限可查，不限制
            return "OWNER"
        type_level = principal.data_levels.get(AGENT_DATA, "NONE")
        if LEVEL_RANK[type_level] >= LEVEL_RANK["OWNER"]:
            return type_level
        own = await self.uc.data_check(AGENT_DATA, str(agent.id), principal.auth_uid)
        return _max(own, type_level)

    async def visibility(self, principal: Principal) -> Visibility:
        type_level = principal.data_levels.get(AGENT_DATA, "NONE")
        if self.uc is None or LEVEL_RANK[type_level] >= LEVEL_RANK["OWNER"]:
            return Visibility({}, type_level, all_=True)
        levels, type_level = await self.uc.data_accessible(AGENT_DATA, principal.auth_uid)
        return Visibility({u: v for k, v in levels.items() if (u := _uuid(k))}, type_level)

    async def require(self, principal: Principal, agent: Agent, need: str | None = None) -> str:
        """看不到 → 404（不泄露存在）；看得到但级别不够 → 403。返回有效级别。"""
        level = await self.level(principal, agent)
        if LEVEL_RANK[level] < LEVEL_RANK[STATUS_MIN.get(agent.status, "OWNER")]:
            raise NotFound(f"智能体 {agent.id} 不存在", agent_id=str(agent.id))
        if need is not None and LEVEL_RANK[level] < LEVEL_RANK[need]:
            raise Forbidden(_DENIED[need])
        return level

    async def require_use(self, principal: Principal, agent: Agent) -> None:
        """在已有会话里继续用（发消息）：使用权收回后立即生效。

        会话是自己的，不必隐藏 Agent → 403 而不是 404。
        """
        level = await self.level(principal, agent)
        if LEVEL_RANK[level] < LEVEL_RANK[STATUS_MIN.get(agent.status, "OWNER")]:
            raise Forbidden("没有使用这个智能体的权限（可能已被收回），请联系它的所有者")


def _uuid(raw: str) -> UUID | None:
    """用户中心里 AtlasAgent 的 data_id 是 agent id；手工录入的脏数据跳过而不是 500。"""
    try:
        return UUID(raw)
    except ValueError:
        return None


_DENIED = {
    "READ": "没有使用这个智能体的权限",
    "WRITE": "没有编辑这个智能体的权限",
    "OWNER": "只有智能体的所有者可以执行这个操作",
}
