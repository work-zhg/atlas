from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..db.models import Agent, AgentVersion, Thread


class AgentRepository:
    """唯一接触 agent / agent_version 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 读

    async def get(self, agent_id: UUID) -> tuple[Agent, AgentVersion] | None:
        cv = aliased(AgentVersion)
        stmt = (
            select(Agent, cv)
            .join(cv, cv.id == Agent.current_version_id)
            .where(Agent.id == agent_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def get_by_slug(self, slug: str) -> Agent | None:
        stmt = select(Agent).where(Agent.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        model: str | None = None,
        limit: int = 50,
    ) -> list[tuple[Agent, AgentVersion]]:
        cv = aliased(AgentVersion)
        stmt = select(Agent, cv).join(cv, cv.id == Agent.current_version_id)

        if status:
            stmt = stmt.where(Agent.status == status)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                or_(
                    Agent.name.ilike(pattern),
                    Agent.slug.ilike(pattern),
                    Agent.description.ilike(pattern),
                )
            )
        if model:
            # spec 是 JSONB，模型 id 在 spec->'model'->>'model'
            stmt = stmt.where(cv.spec["model"]["model"].as_string() == model)

        stmt = stmt.order_by(Agent.updated_at.desc(), Agent.id.desc()).limit(limit)
        return [(r[0], r[1]) for r in (await self._session.execute(stmt)).all()]

    async def list_versions(self, agent_id: UUID) -> list[AgentVersion]:
        stmt = (
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        )
        return list((await self._session.execute(stmt)).scalars())

    # ------------------------------------------------------------------ 写

    async def create(
        self,
        *,
        slug: str,
        name: str,
        description: str,
        avatar_key: str,
        spec: dict[str, Any],
        created_by: UUID,
    ) -> tuple[Agent, AgentVersion]:
        agent = Agent(
            slug=slug,
            name=name,
            description=description,
            avatar_key=avatar_key,
            status="draft",
            created_by=created_by,
        )
        self._session.add(agent)
        await self._session.flush()  # 拿到 agent.id

        version = AgentVersion(agent_id=agent.id, version=1, spec=spec, created_by=created_by)
        self._session.add(version)
        await self._session.flush()

        agent.current_version_id = version.id
        await self._session.flush()
        # created_at / updated_at 是 server_default：flush 后 Python 侧仍是空的，
        # 同步读取会触发隐式 IO → MissingGreenlet。必须显式 refresh。
        await self._session.refresh(agent)
        await self._session.refresh(version)
        return agent, version

    async def add_version(
        self, *, agent: Agent, spec: dict[str, Any], created_by: UUID
    ) -> AgentVersion:
        """保存即产生新版本 —— 从不原地更新（文档 §5.4）。"""
        next_version = await self._next_version(agent.id)
        version = AgentVersion(
            agent_id=agent.id, version=next_version, spec=spec, created_by=created_by
        )
        self._session.add(version)
        await self._session.flush()

        agent.current_version_id = version.id
        agent.updated_at = func.now()
        await self._session.flush()
        await self._session.refresh(agent)
        await self._session.refresh(version)
        return version

    async def _next_version(self, agent_id: UUID) -> int:
        stmt = select(func.coalesce(func.max(AgentVersion.version), 0)).where(
            AgentVersion.agent_id == agent_id
        )
        return int((await self._session.execute(stmt)).scalar_one()) + 1

    async def archive_threads_of(self, agent_id: UUID) -> int:
        """删除 agent 时关联会话保留但置 archived（文档 §11.1）。"""
        stmt = (
            update(Thread)
            .where(Thread.agent_id == agent_id, Thread.status != "archived")
            .values(status="archived")
        )
        return int((await self._session.execute(stmt)).rowcount or 0)

    async def soft_delete(self, agent: Agent) -> None:
        """★ 归档而非物理删除。

        原设计写的是"删除 agent、关联 thread 保留但置 archived"，但这在 schema 上
        自相矛盾且会破坏可复现性：
          1. thread.agent_id 是 NOT NULL 且无 ON DELETE —— 有会话时物理删除直接违反外键；
          2. agent_version 随 agent 级联删除，而 run.agent_version_id 正是靠它
             解释"这条历史会话当时用的哪份配置"（§5.4）。删掉 agent 等于把 §5.4
             要保住的东西亲手毁掉。
        所以 DELETE 的语义落为归档：agent 置 archived，其会话一并归档。
        """
        agent.status = "archived"
        agent.updated_at = func.now()
        await self._session.flush()
        await self._session.refresh(agent)
