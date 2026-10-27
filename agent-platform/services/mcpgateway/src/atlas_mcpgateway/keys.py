"""API Key（MCP 方案设计 §5.5）：一个主体（人或数字员工）最多一把，只表示「我是谁」。

    发放   每个主体一把；同时勾选的服务写的是持有人的服务授权（用户中心），不是「Key 的权限」
    重置   生成新原文，旧的立即失效（Key 泄露时止血）
    停用   立即失效；不提供删除，审计记录永远能对上持有人
    启用   重新生成：停用期间 Key 可能已经泄露，恢复旧值等于把泄露的 Key 一起恢复

★ 原文存库（Atlas 代主体调用时要带原文）。管理端只显示掩码；本期没有人能看原文（Q3），
  数字员工的 Key 永远不显示。原文只经内部接口交给 Atlas。
★ 只改本服务自己的库：发放、重置、停用、启用立即生效，不用同步到 Higress。
"""

from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import McpApiKey
from .db.types import utcnow
from .errors import Conflict, Invalid, NotFound

__all__ = ["KeyStore", "mask"]

_PREFIX = "mk-"


def _generate() -> str:
    return _PREFIX + secrets.token_urlsafe(32)


def mask(api_key: str) -> str:
    return f"{api_key[:7]}…{api_key[-4:]}"


class KeyStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[McpApiKey]:
        return list(await self.session.scalars(select(McpApiKey).order_by(McpApiKey.created_at)))

    async def get(self, key_uuid: UUID) -> McpApiKey:
        row = await self.session.scalar(select(McpApiKey).where(McpApiKey.uuid == key_uuid))
        if row is None:
            raise NotFound("API Key 不存在")
        return row

    async def of_subject(self, subject_uid: UUID) -> McpApiKey | None:
        return await self.session.scalar(
            select(McpApiKey).where(McpApiKey.subject_uid == subject_uid)
        )

    async def authenticate(self, api_key: str) -> McpApiKey | None:
        """数据面：Key → 有效的持有人。停用、重置立即生效（每个请求查库，不缓存）。"""
        return await self.session.scalar(
            select(McpApiKey).where(McpApiKey.api_key == api_key, McpApiKey.status == "active")
        )

    async def issue(
        self, subject_uid: UUID, *, subject_type: str, subject_name: str, actor: str
    ) -> McpApiKey:
        if await self.of_subject(subject_uid) is not None:
            raise Conflict("这个主体已经有 API Key（一个主体最多一把，可以重置或启用）")
        if subject_type not in ("human", "digital"):
            raise Invalid(f"不支持的主体类型 {subject_type}")
        row = McpApiKey(
            subject_uid=subject_uid,
            subject_type=subject_type,
            subject_name=subject_name[:100],
            api_key=_generate(),
            status="active",
            issued_by=actor,
            rotated_at=utcnow(),
        )
        self.session.add(row)
        audit.record(self.session, actor=actor, action="key.issue", target=str(subject_uid))
        await self.session.flush()
        return row

    async def reset(self, key_uuid: UUID, *, actor: str) -> McpApiKey:
        row = await self.get(key_uuid)
        row.api_key, row.rotated_at = _generate(), utcnow()
        audit.record(self.session, actor=actor, action="key.reset", target=str(row.subject_uid))
        return row

    async def disable(self, key_uuid: UUID, *, actor: str) -> McpApiKey:
        row = await self.get(key_uuid)
        row.status, row.disabled_at = "disabled", utcnow()
        audit.record(self.session, actor=actor, action="key.disable", target=str(row.subject_uid))
        return row

    async def enable(self, key_uuid: UUID, *, actor: str) -> McpApiKey:
        row = await self.get(key_uuid)
        if row.status == "active":
            return row
        row.status, row.disabled_at = "active", None
        row.api_key, row.rotated_at = _generate(), utcnow()
        audit.record(self.session, actor=actor, action="key.enable", target=str(row.subject_uid))
        return row
