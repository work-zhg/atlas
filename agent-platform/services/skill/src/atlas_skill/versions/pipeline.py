"""PublishPipeline：审查通过的上传 → 不可变的已发布版本。

    锁技能行 → 分配版本号（当前最大 + 1）→ 技能仓库拷到发布区并核对 → 改行（版本号、状态、存储位置）

★ 草稿不在这里删：返回草稿前缀，由 SkillService 在**提交之后**删。提交前删的话，提交失败时行回滚成
  待审查、草稿却没了。
"""

from __future__ import annotations

from .. import audit
from ..db.models import SkillVersion
from ..store.store import SkillStore
from .repository import SkillRepository

__all__ = ["PublishPipeline"]


class PublishPipeline:
    def __init__(self, repo: SkillRepository, store: SkillStore) -> None:
        self._repo = repo
        self._store = store

    async def publish(self, row: SkillVersion, *, actor: str) -> str:
        """→ 草稿前缀（提交之后再删）。"""
        await self._repo.lock_skill(row.slug)
        version = await self._repo.max_version(row.skill) + 1
        draft = row.storage_key
        dst = await self._store.publish(draft, row.slug, version, row.file_count)
        row.publish_as(version, dst, actor=actor)
        audit.record(
            self._repo.session,
            actor=actor,
            action="skill.publish",
            target_kind="skill",
            target_id=row.slug,
            version=version,
            content_hash=row.content_hash,
        )
        await self._repo.session.flush()
        return draft
