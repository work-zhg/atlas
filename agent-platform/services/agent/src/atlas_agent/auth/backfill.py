"""存量 Agent 回填（数字员工设计 §12）：迁移 0023 之后执行一次，可重复执行。

对每个 Agent：
  1. 没有数字员工的补建（幂等，external_ref = agent id）。负责人 = 创建者
  2. 还没在用户中心登记的：创建者 = OWNER；已启用的再给根部门（含下级）READ ——
     保持上线前「人人可用」不突变（数字员工设计 §8.1）。已有授权的不动
权限类型不动：存量一律以人为权限（迁移 0023 的缺省），行为与上线前相同。

★ 每个 Agent 以它的负责人身份调用户中心（X-Uid）：租户按负责人推（内网身份传递 §11）。
★ 单个失败（如创建者在用户中心已不存在）记下来继续，最后汇总。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ..db.models import Agent
from ..db.session import get_sessionmaker
from ..outbound import CallIdentity, use_identity
from .catalog import AGENT_DATA
from .digital import AgentIdentity
from .uc import UCClient, UCError

__all__ = ["backfill_agent_identity"]

log = logging.getLogger("atlas_agent.auth")


async def backfill_agent_identity(uc: UCClient, *, dry_run: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"digital_created": 0, "registered": 0, "failed": []}
    identity = AgentIdentity(uc)
    roots: dict[str, str] = {}  # 租户（按负责人）→ 根部门；同一部署通常只有一个
    async with get_sessionmaker()() as session:
        agents = list(await session.scalars(select(Agent).order_by(Agent.created_at)))
        for agent in agents:
            owner = agent.created_by
            use_identity(CallIdentity(uid=owner, auth_uid=owner))
            try:
                if agent.digital_employee_uid is None:
                    if not dry_run:
                        await identity.ensure_digital_employee(agent, owner=owner)
                    out["digital_created"] += 1
                if not await uc.data_acl(AGENT_DATA, str(agent.id)):
                    grants = [
                        {"permission": "OWNER", "subject_type": "USER", "subject_id": str(owner)}
                    ]
                    if agent.status == "enabled":
                        root = roots.get(str(owner)) or await _root_dept(uc)
                        roots[str(owner)] = root
                        grants.append(
                            {
                                "permission": "READ",
                                "subject_type": "DEPT",
                                "subject_id": root,
                                "include_sub": True,
                            }
                        )
                    if not dry_run:
                        await uc.data_grant(AGENT_DATA, str(agent.id), grants, data_name=agent.name)
                    out["registered"] += 1
                if not dry_run:
                    await session.commit()
            except UCError as exc:
                await session.rollback()
                out["failed"].append(f"{agent.slug}：{exc.message}")
                log.warning("回填失败 agent=%s：%s", agent.slug, exc.message)
            finally:
                use_identity(None)
    return out


async def _root_dept(uc: UCClient) -> str:
    tree = await uc.depts_tree()
    return next(str(d["id"]) for d in tree if not d.get("parent_id"))
