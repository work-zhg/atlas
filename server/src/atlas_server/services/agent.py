"""智能体服务（文档 §11.1）。

核心语义：**保存即产生新版本**。PATCH 从不原地改 spec，而是写一条
agent_version 并切换 current_version_id —— 这样每条历史 run 都能指回
"当时用的哪份配置"（§5.4）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Agent, AgentVersion
from ..errors import BuiltinAgentProtected, NotFound, SlugTaken
from ..repositories.agent import AgentRepository
from ..schemas.agent import (
    AgentCreate,
    AgentDetailOut,
    AgentListOut,
    AgentOut,
    AgentSpecIn,
    AgentUpdate,
    AgentVersionListOut,
    AgentVersionOut,
)


def _summarize(agent: Agent, version: AgentVersion) -> AgentOut:
    spec: dict[str, Any] = version.spec
    model = (spec.get("model") or {}).get("model", "")
    return AgentOut(
        id=agent.id,
        slug=agent.slug,
        name=agent.name,
        description=agent.description,
        avatar_key=agent.avatar_key,
        status=agent.status,  # type: ignore[arg-type]
        is_builtin=agent.is_builtin,
        version=version.version,
        model=model,
        tool_count=len(spec.get("tool_names") or []),
        subagent_count=len(spec.get("subagents") or []),
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


class AgentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AgentRepository(session)

    # ------------------------------------------------------------------ 读

    async def list(
        self, *, q: str | None, status: str | None, model: str | None, limit: int
    ) -> AgentListOut:
        rows = await self._repo.list(q=q, status=status, model=model, limit=limit)
        return AgentListOut(data=[_summarize(a, v) for a, v in rows])

    async def get(self, agent_id: UUID) -> AgentDetailOut:
        agent, version = await self._require(agent_id)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def list_versions(self, agent_id: UUID) -> AgentVersionListOut:
        await self._require(agent_id)
        versions = await self._repo.list_versions(agent_id)
        return AgentVersionListOut(
            data=[
                AgentVersionOut(id=v.id, version=v.version, spec=v.spec, created_at=v.created_at)
                for v in versions
            ]
        )

    # ------------------------------------------------------------------ 写

    async def create(self, payload: AgentCreate, *, user_id: UUID) -> AgentDetailOut:
        if await self._repo.get_by_slug(payload.slug):
            raise SlugTaken(f"标识 {payload.slug!r} 已被占用", slug=payload.slug)

        # ★ 在这里就跑 engine 的校验：opus-5 传 temperature 之类的错误
        #   在点保存时被拒，而不是等到某次 run（§3 D3）
        payload.spec.to_engine(slug=payload.slug, name=payload.name).validate()

        agent, version = await self._repo.create(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            avatar_key=payload.avatar_key,
            spec=payload.spec.model_dump(mode="json"),
            created_by=user_id,
        )
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def update(
        self, agent_id: UUID, payload: AgentUpdate, *, user_id: UUID
    ) -> AgentDetailOut:
        agent, current = await self._require(agent_id)

        if payload.name is not None:
            agent.name = payload.name
        if payload.description is not None:
            agent.description = payload.description
        if payload.avatar_key is not None:
            agent.avatar_key = payload.avatar_key

        version = current
        if payload.spec is not None:
            payload.spec.to_engine(slug=agent.slug, name=agent.name).validate()
            version = await self._repo.add_version(
                agent=agent, spec=payload.spec.model_dump(mode="json"), created_by=user_id
            )
        else:
            # 只改了元信息也要刷新 updated_at，列表页按它排序
            await self._session.flush()

        await self._session.refresh(agent)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def set_status(self, agent_id: UUID, status: str) -> AgentDetailOut:
        agent, version = await self._require(agent_id)
        agent.status = status
        await self._session.flush()
        await self._session.refresh(agent)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def delete(self, agent_id: UUID) -> dict[str, Any]:
        """归档语义，不是物理删除 —— 理由见 AgentRepository.soft_delete。"""
        agent, _ = await self._require(agent_id)
        if agent.is_builtin:
            raise BuiltinAgentProtected(f"{agent.slug!r} 是内置智能体，不可删除", slug=agent.slug)
        archived = await self._repo.archive_threads_of(agent.id)
        await self._repo.soft_delete(agent)
        return {"archived": True, "archived_threads": archived}

    # ------------------------------------------------------------------ 内部

    async def _require(self, agent_id: UUID) -> tuple[Agent, AgentVersion]:
        found = await self._repo.get(agent_id)
        if found is None:
            raise NotFound(f"智能体 {agent_id} 不存在", agent_id=str(agent_id))
        return found


__all__ = ["AgentService", "AgentSpecIn"]
