"""SkillRepository：读写 skill、skill_version。

★ 上传、发布时锁技能行（FOR UPDATE），审查、改状态时只锁版本行；运行时的查询结构性地只取已发布、
  停用、下架的版本。
★ 版本行按 skill_uuid 引用技能（关系 SkillVersion.skill 一起查出来，slug 从它来）。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import RUNTIME_VISIBLE, Skill, SkillVersion
from ..errors import NotFound

__all__ = ["SkillRepository"]


class SkillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── 技能

    async def skill(self, slug: str) -> Skill | None:
        return await self.session.scalar(select(Skill).where(Skill.slug == slug))

    async def lock_skill(self, slug: str) -> Skill | None:
        return await self.session.scalar(select(Skill).where(Skill.slug == slug).with_for_update())

    async def skills(self) -> list[Skill]:
        return list(await self.session.scalars(select(Skill).order_by(Skill.slug)))

    # ── 版本

    async def by_upload(self, upload_id: UUID, *, lock: bool = False) -> SkillVersion:
        stmt = select(SkillVersion).where(SkillVersion.uuid == upload_id)
        if lock:
            stmt = stmt.with_for_update(of=SkillVersion)
        row = await self.session.scalar(stmt)
        if row is None:
            raise NotFound(f"上传 {upload_id} 不存在")
        return row

    async def version(self, slug: str, version: int, *, lock: bool = False) -> SkillVersion:
        stmt = (
            select(SkillVersion)
            .join(Skill, Skill.uuid == SkillVersion.skill_uuid)
            .where(Skill.slug == slug, SkillVersion.version == version)
        )
        if lock:
            stmt = stmt.with_for_update(of=SkillVersion)
        row = await self.session.scalar(stmt)
        if row is None:
            raise NotFound(f"技能 {slug} v{version} 不存在")
        return row

    async def released(self, slug: str, version: int) -> SkillVersion:
        """运行时看得见的一个版本（已发布、停用、下架）。"""
        row = await self.version(slug, version)
        if not row.runtime_visible:
            raise NotFound(f"技能 {slug} v{version} 不存在")
        return row

    async def pending_of(self, skill: Skill) -> UUID | None:
        return await self.session.scalar(
            select(SkillVersion.uuid).where(
                SkillVersion.skill_uuid == skill.uuid, SkillVersion.status == "pending_review"
            )
        )

    async def same_content(self, skill: Skill, content_hash: str) -> int | None:
        """和这个技能已发布的哪一版内容完全相同。"""
        return await self.session.scalar(
            select(SkillVersion.version).where(
                SkillVersion.skill_uuid == skill.uuid,
                SkillVersion.content_hash == content_hash,
                SkillVersion.status.in_(RUNTIME_VISIBLE),
            )
        )

    async def max_version(self, skill: Skill) -> int:
        current = await self.session.scalar(
            select(func.max(SkillVersion.version)).where(SkillVersion.skill_uuid == skill.uuid)
        )
        return int(current or 0)

    async def versions(
        self, skill: Skill | None = None, *, runtime: bool = False
    ) -> list[SkillVersion]:
        """一个技能（缺省 = 全部技能）的版本，按创建顺序。runtime：只要运行时看得见的。"""
        stmt = select(SkillVersion).order_by(SkillVersion.created_at, SkillVersion.id)
        if skill is not None:
            stmt = stmt.where(SkillVersion.skill_uuid == skill.uuid)
        if runtime:
            stmt = stmt.where(SkillVersion.status.in_(RUNTIME_VISIBLE))
        return list(await self.session.scalars(stmt))

    async def revoked(self) -> list[SkillVersion]:
        return list(
            await self.session.scalars(select(SkillVersion).where(SkillVersion.status == "revoked"))
        )

    async def published_descriptions(self, except_skill: Skill | None) -> dict[str, str]:
        """其它技能已发布的最新描述（描述冲突检查用）。"""
        stmt = (
            select(SkillVersion)
            .where(SkillVersion.status == "published")
            .order_by(SkillVersion.version)
        )
        if except_skill is not None:
            stmt = stmt.where(SkillVersion.skill_uuid != except_skill.uuid)
        return {r.slug: r.description for r in await self.session.scalars(stmt)}  # 后者覆盖 = 最新
