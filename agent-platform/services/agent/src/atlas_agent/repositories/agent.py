from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..db.models import Agent, AgentVersion, AppUser, Thread


class AgentRepository:
    """唯一接触 agent / agent_version 表的地方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ 读

    async def get(self, agent_id: UUID) -> tuple[Agent, AgentVersion] | None:
        cv = aliased(AgentVersion)
        stmt = (
            select(Agent, cv)
            .join(cv, cv.agent_version_uuid == Agent.current_version_uuid)
            .where(Agent.agent_uuid == agent_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def version(self, version_id: UUID) -> AgentVersion | None:
        stmt = select(AgentVersion).where(AgentVersion.agent_version_uuid == version_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
        visible: Any = None,
        created_by: UUID | None = None,
        not_created_by: UUID | None = None,
    ) -> builtins.list[tuple[Agent, AgentVersion]]:
        """visible：可见范围的 SQL 条件（auth/agent_access.Visibility）。None = 不过滤。

        created_by / not_created_by：「我创建的」/「共享给我的」。
        """
        cv = aliased(AgentVersion)
        stmt = select(Agent, cv).join(cv, cv.agent_version_uuid == Agent.current_version_uuid)
        if visible is not None:
            stmt = stmt.where(visible)
        if created_by is not None:
            stmt = stmt.where(Agent.created_by_uuid == created_by)
        if not_created_by is not None:
            stmt = stmt.where(Agent.created_by_uuid != not_created_by)

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

        stmt = stmt.order_by(Agent.updated_at.desc(), Agent.agent_uuid.desc()).limit(limit)
        return [(r[0], r[1]) for r in (await self._session.execute(stmt)).all()]

    async def list_versions(self, agent_id: UUID) -> builtins.list[AgentVersion]:
        """发布出的版本（调试预览的快照不算）。"""
        stmt = (
            select(AgentVersion)
            .where(AgentVersion.agent_uuid == agent_id, AgentVersion.kind == "release")
            .order_by(AgentVersion.version.desc())
        )
        return list((await self._session.execute(stmt)).scalars())

    async def owner_names(self, user_ids: builtins.list[UUID]) -> dict[UUID, str]:
        """创建者的姓名（用户中心用户在 Atlas 的投影）。"""
        if not user_ids:
            return {}
        rows = await self._session.execute(
            select(AppUser.user_uuid, AppUser.name).where(AppUser.user_uuid.in_(set(user_ids)))
        )
        return {r[0]: r[1] for r in rows.all()}

    async def last_used(
        self, agent_ids: builtins.list[UUID], *, user_id: UUID
    ) -> dict[UUID, datetime]:
        """这个人最近一次用各个智能体的时间：他自己的会话里最近的更新时间（调试预览不算）。"""
        if not agent_ids:
            return {}
        rows = await self._session.execute(
            select(Thread.agent_uuid, func.max(Thread.updated_at))
            .where(
                Thread.agent_uuid.in_(agent_ids),
                Thread.created_by_uuid == user_id,
                Thread.parent_thread_uuid.is_(None),
                Thread.status != "preview",
            )
            .group_by(Thread.agent_uuid)
        )
        return {r[0]: r[1] for r in rows.all()}

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
        auth_subject: str = "user",
    ) -> tuple[Agent, AgentVersion]:
        agent = Agent(
            slug=slug,
            name=name,
            description=description,
            avatar_key=avatar_key,
            status="draft",
            created_by_uuid=created_by,
            auth_subject=auth_subject,
        )
        self._session.add(agent)
        await self._session.flush()  # 拿到 agent.id

        version = AgentVersion(
            agent_uuid=agent.agent_uuid, version=1, spec=spec, created_by_uuid=created_by
        )
        self._session.add(version)
        await self._session.flush()

        agent.current_version_uuid = version.agent_version_uuid
        await self._session.flush()
        # created_at / updated_at 是 server_default：flush 后 Python 侧仍是空的，
        # 同步读取会触发隐式 IO → MissingGreenlet。必须显式 refresh。
        await self._session.refresh(agent)
        await self._session.refresh(version)
        return agent, version

    async def add_version(
        self, *, agent: Agent, spec: dict[str, Any], created_by: UUID, note: str = ""
    ) -> AgentVersion:
        """保存即产生新版本 —— 从不原地更新（文档 §5.4）。"""
        next_version = await self._next_version(agent.agent_uuid)
        version = AgentVersion(
            agent_uuid=agent.agent_uuid,
            version=next_version,
            spec=spec,
            created_by_uuid=created_by,
            note=note,
        )
        self._session.add(version)
        await self._session.flush()

        agent.current_version_uuid = version.agent_version_uuid
        agent.updated_at = func.now()
        await self._session.flush()
        await self._session.refresh(agent)
        await self._session.refresh(version)
        return version

    async def save_draft(self, agent: Agent, spec: dict[str, Any] | None) -> None:
        """草稿：自动保存，不产生版本。None = 清掉（发布之后、或放弃修改）。"""
        agent.draft_spec = spec
        agent.draft_updated_at = datetime.now(UTC) if spec is not None else None
        await self._session.flush()

    async def preview_version(
        self, *, agent: Agent, spec: dict[str, Any], created_by: UUID
    ) -> AgentVersion:
        """调试预览用的快照：run 必须指向一份固定的配置（§5.4），草稿却随时在改。

        ★ 不切换 current_version、不进版本历史；版本号取负数，不占发布版本的编号。
        ★ 和上一个快照一样就复用：预览时连发几条消息不该拍一串一样的快照。
        """
        last = (
            await self._session.execute(
                select(AgentVersion)
                .where(AgentVersion.agent_uuid == agent.agent_uuid, AgentVersion.kind == "preview")
                .order_by(AgentVersion.version.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last is not None and last.spec == spec:
            return last
        lowest = (
            await self._session.execute(
                select(func.coalesce(func.min(AgentVersion.version), 0)).where(
                    AgentVersion.agent_uuid == agent.agent_uuid
                )
            )
        ).scalar_one()
        version = AgentVersion(
            agent_uuid=agent.agent_uuid,
            version=min(int(lowest), 0) - 1,
            spec=spec,
            created_by_uuid=created_by,
            kind="preview",
        )
        self._session.add(version)
        await self._session.flush()
        await self._session.refresh(version)
        return version

    async def _next_version(self, agent_id: UUID) -> int:
        stmt = select(func.coalesce(func.max(AgentVersion.version), 0)).where(
            AgentVersion.agent_uuid == agent_id
        )
        return int((await self._session.execute(stmt)).scalar_one()) + 1

    async def archive_threads_of(self, agent_id: UUID) -> int:
        """删除 agent 时关联会话保留但置 archived（文档 §11.1）。"""
        stmt = (
            update(Thread)
            .where(Thread.agent_uuid == agent_id, Thread.status != "archived")
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
