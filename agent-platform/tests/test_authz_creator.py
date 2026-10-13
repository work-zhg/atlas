"""创建者规则对服务账号同样适用：服务账号能用、能改自己建的智能体，别人的不行。

背景：有 agent:create 的服务账号（如评测账号 atlas-bench）建的智能体是草稿，
而服务账号只能用已启用的、且原规则只认「用户身份」的创建者 —— 建完既用不了也启用不了。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from atlas_server.auth.authz import can_use_agent, ensure_agent_editable
from atlas_server.auth.principal import Principal
from atlas_server.db.models import Agent
from atlas_server.errors import Forbidden, NotFound

SA = uuid4()
OTHER = uuid4()


def _sa(**kw: Any) -> Principal:
    return Principal(kind="service", owner_id=SA, agent_scope="all_enabled", **kw)


def _agent(created_by: Any, status: str = "draft") -> Agent:
    return Agent(id=uuid4(), status=status, created_by=created_by, is_builtin=False)


class _Session:
    def __init__(self, agent: Agent) -> None:
        self._a = agent

    async def get(self, _model: Any, _id: Any) -> Agent:
        return self._a


def test_service_account_can_use_its_own_draft() -> None:
    assert can_use_agent(_sa(), _agent(SA, "draft"))
    assert not can_use_agent(_sa(), _agent(OTHER, "draft"))
    assert can_use_agent(_sa(), _agent(OTHER, "enabled"))  # 原有范围规则不变


async def test_service_account_can_edit_only_its_own() -> None:
    mine = _agent(SA)
    await ensure_agent_editable(_Session(mine), mine.id, _sa())  # type: ignore[arg-type]
    theirs = _agent(OTHER, "enabled")
    with pytest.raises(Forbidden):
        await ensure_agent_editable(_Session(theirs), theirs.id, _sa())  # type: ignore[arg-type]
    hidden = _agent(OTHER, "draft")  # 看不到的草稿 → 404，而不是暴露它的存在
    with pytest.raises(NotFound):
        await ensure_agent_editable(_Session(hidden), hidden.id, _sa())  # type: ignore[arg-type]


async def test_user_creator_rule_unchanged() -> None:
    user = Principal(kind="user", owner_id=OTHER)
    a = _agent(OTHER)
    await ensure_agent_editable(_Session(a), a.id, user)  # type: ignore[arg-type]
    with pytest.raises(Forbidden):
        b = _agent(SA)
        await ensure_agent_editable(_Session(b), b.id, user)  # type: ignore[arg-type]
