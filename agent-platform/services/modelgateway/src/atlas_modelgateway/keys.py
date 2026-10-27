"""主体 Key（模型管理方案设计 §6.5）：一个主体（人或数字员工）最多一把，只表示「我是谁」。

    生成   第一次被取用时自动生成（issued_by = system），不需要手工发放 —— 能用 Atlas 的人都要用
           模型，手工发放漏一个就有人用不了；Key 不带权限，自动生成不会扩大任何人的权限
    重置   生成新原文，旧的立即失效（Key 泄露时止血）
    停用   立即失效；停用后不会再自动生成；不提供删除，审计记录永远能对上持有人
    启用   重新生成：停用期间 Key 可能已经泄露，恢复旧值等于把泄露的 Key 一起恢复

★ 和 MCP 网关的 Key 分开（§6.5）：一把泄露不影响另一把。
★ 原文存库（Atlas 代主体调用时要带原文）。管理端只显示掩码；本期没有人能看原文，
  数字员工的 Key 永远不显示。原文只经内部接口交给 Atlas。
"""

from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import GwModelKey
from .db.types import utcnow
from .errors import NotFound
from .perms import SubjectInfo

__all__ = ["SYSTEM", "KeyStore", "mask"]

_PREFIX = "gk-"
#: 自动生成时记的操作人
SYSTEM = "system"


def _generate() -> str:
    return _PREFIX + secrets.token_urlsafe(32)


def mask(api_key: str) -> str:
    return f"{api_key[:7]}…{api_key[-4:]}"


class KeyStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[GwModelKey]:
        return list(await self.session.scalars(select(GwModelKey).order_by(GwModelKey.created_at)))

    async def get(self, key_uuid: UUID) -> GwModelKey:
        row = await self.session.scalar(select(GwModelKey).where(GwModelKey.uuid == key_uuid))
        if row is None:
            raise NotFound("Key 不存在")
        return row

    async def of_subject(self, subject_uid: UUID) -> GwModelKey | None:
        return await self.session.scalar(
            select(GwModelKey).where(GwModelKey.subject_uid == subject_uid)
        )

    async def authenticate(self, api_key: str) -> GwModelKey | None:
        """数据面：Key → 有效的持有人。停用、重置立即生效（每个请求查库）。"""
        return await self.session.scalar(
            select(GwModelKey).where(GwModelKey.api_key == api_key, GwModelKey.status == "active")
        )

    async def ensure(self, subject: SubjectInfo) -> GwModelKey:
        """主体的 Key；还没有就生成一把。停用的原样返回（不会因此重新生成）。

        ★ 并发的两次「第一次取用」会撞唯一约束：输的那次回滚到保存点，读赢的那把。
        """
        uid = UUID(subject.uid)
        row = await self.of_subject(uid)
        if row is not None:
            return row
        row = GwModelKey(
            subject_uid=uid,
            subject_type=subject.user_type
            if subject.user_type in ("human", "digital")
            else "human",
            subject_name=subject.name[:100],
            api_key=_generate(),
            status="active",
            issued_by=SYSTEM,
            rotated_at=utcnow(),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            found = await self.of_subject(uid)
            if found is None:
                raise
            return found
        audit.record(self.session, actor=SYSTEM, action="key.issue", target=subject.uid)
        return row

    async def reset(self, key_uuid: UUID, *, actor: str) -> GwModelKey:
        row = await self.get(key_uuid)
        row.api_key, row.rotated_at = _generate(), utcnow()
        audit.record(self.session, actor=actor, action="key.reset", target=str(row.subject_uid))
        return row

    async def disable(self, key_uuid: UUID, *, actor: str) -> GwModelKey:
        row = await self.get(key_uuid)
        row.status, row.disabled_at = "disabled", utcnow()
        audit.record(self.session, actor=actor, action="key.disable", target=str(row.subject_uid))
        return row

    async def enable(self, key_uuid: UUID, *, actor: str) -> GwModelKey:
        row = await self.get(key_uuid)
        if row.status == "active":
            return row
        row.status, row.disabled_at = "active", None
        row.api_key, row.rotated_at = _generate(), utcnow()
        audit.record(self.session, actor=actor, action="key.enable", target=str(row.subject_uid))
        return row
