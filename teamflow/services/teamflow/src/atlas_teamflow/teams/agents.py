"""AgentDirectory：调 Agent 平台的智能体接口（防腐层）。

    list / detail   本租户里这个人看得到的智能体；团队只接以数字员工为权限的（auth_subject = agent）
    my_level        这个人对智能体的级别（接入人必须是 Owner）
    share_use       以接入人的身份给一批人授「使用」：已经有授权的人跳过 —— 用户中心对已有的行是改
                    级别，直接授会把 Owner 降成「使用」

★ 智能体的技能、模型、知识库都在 Agent 平台里配，这里每次现读，不存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ..agentplatform import AgentPlatform
from ..outbound import acting_as

__all__ = ["AgentCard", "AgentDirectory"]


@dataclass(frozen=True)
class AgentCard:
    atlas_agent_id: str
    name: str
    kind: str
    model: str | None
    auth_subject: str
    description: str | None
    status: str
    skills: list[str] = field(default_factory=list)

    @property
    def is_digital_employee(self) -> bool:
        return self.auth_subject == "agent"

    @property
    def available(self) -> bool:
        return self.status == "enabled" and self.is_digital_employee


def _card(a: dict[str, Any]) -> AgentCard:
    spec = a.get("spec") or {}
    skills = [
        str(s.get("name") or s.get("slug"))
        for s in spec.get("skills") or []
        if isinstance(s, dict) and (s.get("name") or s.get("slug"))
    ]
    return AgentCard(
        atlas_agent_id=str(a["id"]),
        name=str(a.get("name") or ""),
        kind=str(a.get("kind") or "native"),
        model=a.get("model"),
        auth_subject=str(a.get("auth_subject") or "user"),
        description=a.get("description"),
        status=str(a.get("status") or ""),
        skills=skills,
    )


class AgentDirectory:
    def __init__(self, atlas: AgentPlatform) -> None:
        self.atlas = atlas

    async def list(self, q: str | None = None) -> list[AgentCard]:
        """当前用户看得到的、以数字员工为权限的、已启用的智能体。"""
        return [c for c in map(_card, await self.atlas.list_agents(q)) if c.available]

    async def detail(self, atlas_agent_id: str) -> AgentCard | None:
        a = await self.atlas.get_agent(atlas_agent_id)
        return _card(a) if a else None

    async def my_level(self, atlas_agent_id: str) -> str:
        return str((await self.atlas.agent_grants(atlas_agent_id)).get("my_level") or "NONE")

    async def share_use(
        self, atlas_agent_id: str, uids: list[UUID], *, operator: UUID
    ) -> list[UUID]:
        """以 operator（接入人）的身份授「使用」；返回这次真正授了的人。"""
        with acting_as(operator):
            rows = (await self.atlas.agent_grants(atlas_agent_id)).get("data") or []
            have = {
                str((r.get("subject") or {}).get("id"))
                for r in rows
                if (r.get("subject") or {}).get("type") in ("user", "USER")
            }
            todo = [u for u in dict.fromkeys(uids) if str(u) not in have]
            if todo:
                await self.atlas.grant_use(atlas_agent_id, todo)
        return todo
