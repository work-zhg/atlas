"""智能体的编辑权：创建者，或「全部智能体 · Owner」（数据编码级授权）；内置智能体只有后者。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from atlas_agent.auth.authz import ensure_agent_editable
from atlas_agent.auth.principal import Principal
from atlas_agent.db.models import Agent
from atlas_agent.errors import Forbidden, NotFound

ME = uuid4()
OTHER = uuid4()


def _agent(created_by: Any, status: str = "draft") -> Agent:
    return Agent(id=uuid4(), status=status, created_by=created_by, is_builtin=False)


class _Session:
    def __init__(self, agent: Agent | None) -> None:
        self._a = agent

    async def get(self, _model: Any, _id: Any) -> Agent | None:
        return self._a


async def test_creator_rule() -> None:
    me = Principal(uid=ME)
    mine = _agent(ME)
    await ensure_agent_editable(_Session(mine), mine.id, me)  # type: ignore[arg-type]
    theirs = _agent(OTHER, "enabled")
    with pytest.raises(Forbidden):
        await ensure_agent_editable(_Session(theirs), theirs.id, me)  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await ensure_agent_editable(_Session(None), uuid4(), me)  # type: ignore[arg-type]


async def test_managing_all_agents_is_a_data_permission_not_a_role() -> None:
    """「管理全部智能体」= 用户中心的数据编码级授权「全部智能体 · Owner」，不是角色上的操作码。"""
    theirs = _agent(ME, "enabled")
    builtin = Agent(id=uuid4(), status="enabled", created_by=None, is_builtin=True)

    # 角色里带旧操作码也没用 —— 它已不是判定依据
    by_role = Principal(uid=OTHER, operations=frozenset({"agent:manage_all"}))
    with pytest.raises(Forbidden):
        await ensure_agent_editable(_Session(theirs), theirs.id, by_role)  # type: ignore[arg-type]

    # 只有「全部智能体 · 只读 / 读写」不够管理
    reader = Principal(uid=OTHER, data_levels={"AtlasAgent": "WRITE"})
    with pytest.raises(Forbidden):
        await ensure_agent_editable(_Session(theirs), theirs.id, reader)  # type: ignore[arg-type]

    owner = Principal(uid=OTHER, data_levels={"AtlasAgent": "OWNER"})
    await ensure_agent_editable(_Session(theirs), theirs.id, owner)  # type: ignore[arg-type]
    await ensure_agent_editable(_Session(builtin), builtin.id, owner)  # type: ignore[arg-type]


def test_auth_uid_defaults_to_uid_and_apps_are_not_humans() -> None:
    p = Principal(uid=ME)
    assert p.auth_uid == ME and p.app is None
    assert Principal(uid=ME, caller_app="gateway").app is None
    assert Principal(uid=ME, caller_app="teamflow").app == "teamflow"
    assert Principal(uid=ME, auth_uid=OTHER).auth_uid == OTHER
