"""沙箱基座 · 生命周期的三张表（c4-model/agent/sandbox/code-sandbox-base-lifecycle.html）。

TemplateRepository         sandbox_template：每个模板的当前内容，按 code 取
SandboxInstanceRepository  sandbox_instance：实例登记（令牌一起存），删沙箱时整行删除
SandboxEventLog            sandbox_event：生命周期日志，只追加；实例删了日志还在
"""

from __future__ import annotations

import builtins
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import SandboxEvent, SandboxInstance, SandboxTemplate
from ..db.session import fetch_one

__all__ = ["SandboxEventLog", "SandboxInstanceRepository", "TemplateRepository"]


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, code: str) -> SandboxTemplate | None:
        return await fetch_one(self._session, SandboxTemplate, SandboxTemplate.code == code)

    async def by_uuid(self, template_uuid: UUID) -> SandboxTemplate | None:
        return await fetch_one(
            self._session, SandboxTemplate, SandboxTemplate.template_uuid == template_uuid
        )

    async def list(self) -> builtins.list[SandboxTemplate]:
        rows = await self._session.scalars(select(SandboxTemplate).order_by(SandboxTemplate.code))
        return builtins.list(rows)

    def add(self, template: SandboxTemplate) -> None:
        self._session.add(template)


class SandboxInstanceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, instance: SandboxInstance) -> None:
        self._session.add(instance)

    async def get(self, sandbox_uuid: UUID) -> SandboxInstance | None:
        return await fetch_one(
            self._session, SandboxInstance, SandboxInstance.sandbox_uuid == sandbox_uuid
        )

    async def by_external(self, external_id: str) -> SandboxInstance | None:
        return await fetch_one(
            self._session, SandboxInstance, SandboxInstance.external_id == external_id
        )

    async def list(self) -> builtins.list[SandboxInstance]:
        rows = await self._session.scalars(select(SandboxInstance).order_by(SandboxInstance.id))
        return builtins.list(rows)

    async def remove(self, instance: SandboxInstance) -> None:
        await self._session.delete(instance)


class SandboxEventLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def append(self, event: SandboxEvent) -> None:
        self._session.add(event)

    async def history(self, sandbox_uuid: UUID) -> builtins.list[SandboxEvent]:
        rows = await self._session.scalars(
            select(SandboxEvent)
            .where(SandboxEvent.sandbox_uuid == sandbox_uuid)
            .order_by(SandboxEvent.occurred_at, SandboxEvent.id)
        )
        return builtins.list(rows)

    async def between(self, since: datetime, until: datetime) -> builtins.list[SandboxEvent]:
        rows = await self._session.scalars(
            select(SandboxEvent)
            .where(SandboxEvent.occurred_at >= since, SandboxEvent.occurred_at < until)
            .order_by(SandboxEvent.occurred_at, SandboxEvent.id)
        )
        return builtins.list(rows)

    async def purge(self, before: datetime) -> int:
        result = await self._session.execute(
            delete(SandboxEvent).where(SandboxEvent.occurred_at < before)
        )
        return int(getattr(result, "rowcount", 0) or 0)
