"""人 → Agent 的数据权限（数字员工设计 §8.1）：用户中心 AtlasAgent 的级别叠加 Agent 状态。

看得到：已启用要 READ、草稿要 WRITE、已归档要 OWNER；编辑以人为权限的要 WRITE、以 Agent 为
权限的要 OWNER。没有内置智能体（迁移 0024）。
「管理全部智能体」是数据编码级授权，不是角色上的操作码。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_agent.auth.agent_access import AgentAccess, edit_level
from atlas_agent.auth.principal import Principal
from atlas_agent.db.models import Agent
from atlas_agent.errors import Forbidden, NotFound

ME = uuid4()
OTHER = uuid4()


def _agent(status: str = "enabled", *, subject: str = "user") -> Agent:
    return Agent(id=uuid4(), status=status, created_by=ME, auth_subject=subject)


class _UC:
    """只回答 data_check / data_accessible：(user, agent) → 级别。"""

    def __init__(self, levels: dict[tuple[UUID, str], str]) -> None:
        self.levels = levels

    async def data_check(self, code: str, data_id: str, user_id: UUID) -> str:
        assert code == "AtlasAgent"
        return self.levels.get((user_id, data_id), "NONE")

    async def data_accessible(self, code: str, user_id: UUID, **_: Any) -> Any:
        return {d: lv for (u, d), lv in self.levels.items() if u == user_id}, "NONE"


async def test_levels_follow_status() -> None:
    mine, draft, archived = _agent(), _agent("draft"), _agent("archived")
    uc = _UC(
        {
            (ME, str(mine.id)): "READ",
            (ME, str(draft.id)): "READ",
            (ME, str(archived.id)): "WRITE",
        }
    )
    access = AgentAccess(uc)  # type: ignore[arg-type]
    me = Principal(uid=ME)
    assert await access.require(me, mine) == "READ"
    # 草稿要 WRITE 才看得到、已归档要 OWNER：看不到一律 404
    with pytest.raises(NotFound):
        await access.require(me, draft)
    with pytest.raises(NotFound):
        await access.require(me, archived)
    # 看得到但不够改 → 403
    with pytest.raises(Forbidden):
        await access.require(me, mine, edit_level(mine))
    # 别人：看不到
    with pytest.raises(NotFound):
        await access.require(Principal(uid=OTHER), mine)
    # 发消息：收回后 403（会话是自己的，不必隐藏 Agent）
    with pytest.raises(Forbidden):
        await access.require_use(Principal(uid=OTHER), mine)


async def test_editing_agent_subject_needs_owner() -> None:
    """以 Agent 为权限：谁能改提示词和工具，谁就间接握有数字员工的全部权限 → 编辑要 OWNER。"""
    as_user, as_agent = _agent(), _agent(subject="agent")
    assert edit_level(as_user) == "WRITE" and edit_level(as_agent) == "OWNER"
    uc = _UC({(ME, str(as_user.id)): "WRITE", (ME, str(as_agent.id)): "WRITE"})
    access = AgentAccess(uc)  # type: ignore[arg-type]
    me = Principal(uid=ME)
    assert await access.require(me, as_user, edit_level(as_user)) == "WRITE"
    with pytest.raises(Forbidden):
        await access.require(me, as_agent, edit_level(as_agent))


async def test_managing_all_agents_is_a_data_permission_not_a_role() -> None:
    theirs = _agent()
    access = AgentAccess(_UC({}))  # type: ignore[arg-type]

    # 角色里带旧操作码也没用 —— 它已不是判定依据
    by_role = Principal(uid=OTHER, operations=frozenset({"agent:manage_all"}))
    with pytest.raises(NotFound):
        await access.require(by_role, theirs, "WRITE")

    # 「全部智能体 · 读写」能改以人为权限的，不能管理
    writer = Principal(uid=OTHER, data_levels={"AtlasAgent": "WRITE"})
    assert await access.require(writer, theirs, "WRITE") == "WRITE"
    with pytest.raises(Forbidden):
        await access.require(writer, theirs, "OWNER")

    owner = Principal(uid=OTHER, data_levels={"AtlasAgent": "OWNER"})
    assert await access.require(owner, theirs, "OWNER") == "OWNER"


async def test_visibility_condition() -> None:
    """列表的可见范围：一次 accessible，拼成 SQL 条件；全部 · Owner 不过滤。"""
    a = _agent()
    access = AgentAccess(_UC({(ME, str(a.id)): "READ"}))  # type: ignore[arg-type]
    vis = await access.visibility(Principal(uid=ME))
    assert vis.levels == {a.id: "READ"} and not vis.all_
    sql = str(vis.condition().compile(compile_kwargs={"literal_binds": True}))
    assert str(a.id).replace("-", "") in sql.replace("-", "")
    # 没有授权就什么都看不到（没有「内置的人人可见」这一支）
    nothing = await AgentAccess(_UC({})).visibility(Principal(uid=OTHER))  # type: ignore[arg-type]
    assert str(nothing.condition()) == "false"
    owner = await access.visibility(Principal(uid=ME, data_levels={"AtlasAgent": "OWNER"}))
    assert owner.all_


async def test_without_user_center_nothing_is_restricted() -> None:
    """dev 模式 / 不经接口的脚本：没有数据权限可查，不限制。"""
    access = AgentAccess(None)
    assert await access.require(Principal(uid=OTHER), _agent("archived"), "OWNER") == "OWNER"
    assert (await access.visibility(Principal(uid=OTHER))).all_


def test_auth_uid_defaults_to_uid_and_apps_are_not_humans() -> None:
    p = Principal(uid=ME)
    assert p.auth_uid == ME and p.app is None
    assert Principal(uid=ME, caller_app="gateway").app is None
    assert Principal(uid=ME, caller_app="teamflow").app == "teamflow"
    assert Principal(uid=ME, auth_uid=OTHER).auth_uid == OTHER
