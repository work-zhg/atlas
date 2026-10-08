"""技能的状态机与发布流水线（设计 §5.2、§6）。

★ 一份代码两个入口：命令行（__main__）与 HTTP（api/admin.py）都调这里。

存储与数据库的先后顺序是刻意安排的：

  上传   先写草稿对象，再插行。插行失败 → 草稿区留下孤儿对象（无害，可清理）；
         反过来的话会出现「有行无对象」，审查人打开就是 404。
  发布   在 skill 行锁内分配版本号 → 拷贝 → 核对对象数 → 改行 → 删草稿。
         拷贝到一半失败时目标前缀会有残留，但库里没有这个版本；下次发布同号
         之前先清掉残留（目标前缀非空且库里无此版本 = 残留）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..db.models import RUNTIME_VISIBLE, Skill, SkillVersion
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid, NotFound
from ..schemas import (
    RevokedSkill,
    SkillCatalogItem,
    SkillDetailOut,
    SkillVersionAdminOut,
    SkillVersionBrief,
    SkillVersionOut,
)
from ..settings import ConfigSettings
from ..storage import SkillStorage, draft_prefix, published_prefix
from .package import Limits, RawPackage
from .scanner import content_scan, parse, similarity_scan

logger = logging.getLogger(__name__)

__all__ = ["SkillService"]

#: 在线查看文件的上限
FILE_VIEW_MAX_BYTES = 1024 * 1024


def limits_of(settings: ConfigSettings) -> Limits:
    return Limits(
        max_bytes=settings.skill_max_bytes,
        max_files=settings.skill_max_files,
        max_file_bytes=settings.skill_max_file_bytes,
    )


@dataclass
class SkillService:
    session: AsyncSession
    storage: SkillStorage
    settings: ConfigSettings

    # ================================================================ 写

    async def ingest(
        self,
        pkg: RawPackage,
        *,
        source: str,
        actor: str,
        origin_url: str | None = None,
    ) -> SkillVersion:
        """上传一个新版本。builtin 且不含脚本时直接发布，否则进入待审查。

        第 1 层（归档）已在读包时做完；这里做第 2、3 层与描述冲突检查。
        """
        if source not in ("builtin", "tenant", "imported"):
            raise Invalid(f"未知来源 {source!r}")
        if source == "imported" and not origin_url:
            raise Invalid("imported 技能必须给出出处（origin_url）")

        parsed = parse(pkg, description_max=self.settings.skill_description_max_chars)
        skill = await self._lock_skill(parsed.slug)
        if skill is None:
            skill = Skill(slug=parsed.slug, source=source, origin_url=origin_url, created_by=actor)
            self.session.add(skill)
            await self.session.flush()
        elif skill.source != source:
            raise Conflict(
                f"技能 {parsed.slug} 的来源是 {skill.source}，不能以 {source} 上传新版本"
            )

        in_flight = await self.session.scalar(
            select(SkillVersion.id).where(
                SkillVersion.slug == parsed.slug, SkillVersion.status == "pending_review"
            )
        )
        if in_flight is not None:
            raise Conflict(f"技能 {parsed.slug} 已有一个待审查的上传（{in_flight}），先处理它")
        same = await self.session.scalar(
            select(SkillVersion.version).where(
                SkillVersion.slug == parsed.slug,
                SkillVersion.content_hash == parsed.content_hash,
                SkillVersion.status.in_(RUNTIME_VISIBLE),
            )
        )
        if same is not None:
            raise Conflict(f"内容与 {parsed.slug} v{same} 完全相同，不需要发新版本")

        others = await self._other_descriptions(parsed.slug)
        upload_id = uuid4()
        prefix = draft_prefix(str(upload_id))
        await asyncio.to_thread(self.storage.put_tree, prefix, pkg.files)

        row = SkillVersion(
            id=upload_id,
            slug=parsed.slug,
            version=None,
            status="pending_review",
            description=parsed.description,
            frontmatter=parsed.frontmatter,
            storage_key=prefix,
            content_hash=parsed.content_hash,
            size_bytes=parsed.size_bytes,
            file_count=len(parsed.files),
            has_scripts=parsed.has_scripts,
            files=parsed.files,
            scan_result={
                "archive": {"result": "pass", "hits": []},
                "structure": {"result": "pass", "hits": []},
                "content": content_scan(pkg),
                "similarity": similarity_scan(
                    parsed.description, others, threshold=self.settings.skill_similarity_warn
                ),
                "review": None,
                "history": [],
            },
            uploaded_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="skill.upload",
            target_kind="skill",
            target_id=parsed.slug,
            upload_id=str(upload_id),
            content_hash=parsed.content_hash,
            has_scripts=parsed.has_scripts,
        )

        # ★ 含脚本或非 builtin 必须人工审查，没有例外（skill.html §05）
        if not parsed.has_scripts and source == "builtin":
            row.scan_result = {
                **row.scan_result,
                "review": {"result": "skipped", "reason": "builtin 且不含脚本"},
            }
            await self._publish(row, actor=actor)
        return row

    async def review(
        self, upload_id: UUID, *, reviewer: str, approve: bool, note: str
    ) -> SkillVersion:
        row = await self._lock_version(upload_id)
        if row.status != "pending_review":
            raise Conflict(f"该上传当前状态是 {row.status}，不在待审查中")
        if reviewer == row.uploaded_by and not self.settings.allow_self_review:
            raise Forbidden("审查人不能是上传人（四眼原则）")
        now = utcnow()
        row.reviewed_by, row.reviewed_at, row.review_note = reviewer, now, note
        row.scan_result = {
            **row.scan_result,
            "review": {
                "result": "pass" if approve else "reject",
                "by": reviewer,
                "at": now.isoformat(),
                "note": note,
            },
        }
        audit.record(
            self.session,
            actor=reviewer,
            action="skill.approve" if approve else "skill.reject",
            target_kind="skill",
            target_id=row.slug,
            upload_id=str(row.id),
            note=note,
        )
        if approve:
            await self._publish(row, actor=reviewer)
        else:
            row.status = "rejected"
            await self.session.flush()
            await asyncio.to_thread(self.storage.delete_tree, row.storage_key)
        return row

    async def change_status(
        self, slug: str, version: int, *, action: str, actor: str, reason: str | None
    ) -> SkillVersion:
        """停用 / 启用 / 紧急下架。"""
        transitions = {
            "disable": (("published",), "disabled"),
            "enable": (("disabled",), "published"),
            "revoke": (("published", "disabled"), "revoked"),
        }
        if action not in transitions:
            raise Invalid(f"未知操作 {action!r}")
        allowed, target = transitions[action]
        if action == "revoke" and not reason:
            raise Invalid("紧急下架必须写明原因")
        row = await self.session.scalar(
            select(SkillVersion)
            .where(SkillVersion.slug == slug, SkillVersion.version == version)
            .with_for_update()
        )
        if row is None:
            raise NotFound(f"技能 {slug} v{version} 不存在")
        if row.status not in allowed:
            # ★ revoked 不可撤销：要恢复只能发新版本（设计 §5.2）
            raise Conflict(f"{slug} v{version} 当前是 {row.status}，不能 {action}")
        row.status = target
        row.status_reason = reason
        audit.record(
            self.session,
            actor=actor,
            action=f"skill.{action}",
            target_kind="skill",
            target_id=slug,
            version=version,
            reason=reason,
        )
        await self.session.flush()
        return row

    async def adopt(self, slug: str, version: int, *, actor: str) -> SkillVersion:
        """把源仓库里**已有**的包登记进表（P1 上线前手工放进 _skills/ 的技能）。

        只读对象、算哈希，不移动对象 —— 已经引用它的 agent 不受任何影响。
        """
        exists = await self.session.scalar(
            select(SkillVersion.id).where(
                SkillVersion.slug == slug, SkillVersion.version == version
            )
        )
        if exists is not None:
            raise Conflict(f"{slug} v{version} 已经登记过")
        prefix = published_prefix(slug, version)
        files = await asyncio.to_thread(self.storage.get_tree, prefix)
        if not files:
            raise NotFound(f"源仓库里没有 {prefix}")
        pkg = RawPackage(files=files)
        parsed = parse(pkg, description_max=self.settings.skill_description_max_chars)
        if parsed.slug != slug:
            raise Invalid(f"SKILL.md 里的 name 是 {parsed.slug!r}，与目录名 {slug!r} 不一致")
        skill = await self._lock_skill(slug)
        if skill is None:
            self.session.add(Skill(slug=slug, source="builtin", created_by=actor))
        now = utcnow()
        row = SkillVersion(
            slug=slug,
            version=version,
            status="published",
            description=parsed.description,
            frontmatter=parsed.frontmatter,
            storage_key=prefix,
            content_hash=parsed.content_hash,
            size_bytes=parsed.size_bytes,
            file_count=len(parsed.files),
            has_scripts=parsed.has_scripts,
            files=parsed.files,
            scan_result={
                "archive": {"result": "pass", "hits": []},
                "structure": {"result": "pass", "hits": []},
                "content": content_scan(pkg),
                "similarity": {"result": "pass", "hits": []},
                "review": {"result": "adopted", "by": actor, "at": now.isoformat()},
                "history": [],
            },
            uploaded_by=actor,
            reviewed_by=actor,
            reviewed_at=now,
            review_note="登记已有技能（adopt）",
            published_by=actor,
            published_at=now,
        )
        self.session.add(row)
        audit.record(
            self.session,
            actor=actor,
            action="skill.adopt",
            target_kind="skill",
            target_id=slug,
            version=version,
            content_hash=parsed.content_hash,
        )
        await self.session.flush()
        return row

    async def rescan(self, slug: str, version: int, *, actor: str) -> SkillVersion:
        """规则升级后重扫存量版本：结果只追加到 history，不改版本内容。"""
        row = await self._released(slug, version)
        files = await asyncio.to_thread(self.storage.get_tree, row.storage_key)
        result = content_scan(RawPackage(files=files))
        history = [
            *row.scan_result.get("history", []),
            {"at": utcnow().isoformat(), "by": actor, "content": result},
        ]
        row.scan_result = {**row.scan_result, "history": history}
        audit.record(
            self.session,
            actor=actor,
            action="skill.rescan",
            target_kind="skill",
            target_id=slug,
            version=version,
            result=result["result"],
        )
        await self.session.flush()
        return row

    # ================================================================ 读 · 运行时

    async def catalog(self) -> list[SkillCatalogItem]:
        skills = (await self.session.scalars(select(Skill).order_by(Skill.slug))).all()
        rows = (
            await self.session.scalars(
                select(SkillVersion)
                .where(SkillVersion.status.in_(RUNTIME_VISIBLE))
                .order_by(SkillVersion.slug, SkillVersion.version)
            )
        ).all()
        by_slug: dict[str, list[SkillVersion]] = {}
        for row in rows:
            by_slug.setdefault(row.slug, []).append(row)
        items = []
        for skill in skills:
            versions = by_slug.get(skill.slug, [])
            if not versions:
                continue  # 只有待审查 / 被拒的上传 —— 运行时不该知道它存在
            latest = next((v for v in reversed(versions) if v.status == "published"), None)
            items.append(
                SkillCatalogItem(
                    slug=skill.slug,
                    source=skill.source,  # type: ignore[arg-type]
                    latest=latest.version if latest else None,
                    description=latest.description if latest else None,
                    has_scripts=latest.has_scripts if latest else None,
                    versions=[
                        SkillVersionBrief(version=v.version or 0, status=v.status)  # type: ignore[arg-type]
                        for v in versions
                    ],
                )
            )
        return items

    async def runtime_versions(self, slug: str) -> list[SkillVersionOut]:
        source = await self._source(slug)
        rows = (
            await self.session.scalars(
                select(SkillVersion)
                .where(SkillVersion.slug == slug, SkillVersion.status.in_(RUNTIME_VISIBLE))
                .order_by(SkillVersion.version)
            )
        ).all()
        return [_runtime_out(r, source) for r in rows]

    async def runtime_version(self, slug: str, version: int) -> SkillVersionOut:
        row = await self._released(slug, version)
        return _runtime_out(row, await self._source(slug))

    async def revoked(self) -> list[RevokedSkill]:
        rows = (
            await self.session.scalars(select(SkillVersion).where(SkillVersion.status == "revoked"))
        ).all()
        return [
            RevokedSkill(
                slug=r.slug, version=r.version or 0, reason=r.status_reason, at=r.updated_at
            )
            for r in rows
        ]

    # ================================================================ 读 · 管理

    async def admin_list(self) -> list[SkillDetailOut]:
        skills = (await self.session.scalars(select(Skill).order_by(Skill.slug))).all()
        rows = (
            await self.session.scalars(
                select(SkillVersion).order_by(SkillVersion.slug, SkillVersion.created_at)
            )
        ).all()
        by_slug: dict[str, list[SkillVersion]] = {}
        for row in rows:
            by_slug.setdefault(row.slug, []).append(row)
        return [_detail_out(s, by_slug.get(s.slug, [])) for s in skills]

    async def admin_detail(self, slug: str) -> SkillDetailOut:
        skill = await self.session.get(Skill, slug)
        if skill is None:
            raise NotFound(f"技能 {slug} 不存在")
        rows = (
            await self.session.scalars(
                select(SkillVersion)
                .where(SkillVersion.slug == slug)
                .order_by(SkillVersion.created_at)
            )
        ).all()
        return _detail_out(skill, list(rows))

    async def read_file(self, row: SkillVersion, path: str) -> bytes:
        """在线查看包内文件。★ 只认 files 清单里的路径 —— 不让请求拼出任意 key。"""
        meta = next((f for f in row.files if f["path"] == path), None)
        if meta is None:
            raise NotFound(f"包里没有文件 {path}")
        if meta["size"] > FILE_VIEW_MAX_BYTES:
            raise Invalid(f"文件 {meta['size']} 字节，超过在线查看上限")
        if row.status == "rejected":
            raise NotFound("被拒的上传已删除包内容")
        try:
            return await asyncio.to_thread(self.storage.get, f"{row.storage_key}{path}")
        except Exception as exc:
            # ★ 库里有记录、桶里没对象：存储被清过或换了桶。给 404 而不是 500 ——
            #   500 不带 CORS 头，浏览器只会报一句看不出原因的 Failed to fetch。
            logger.warning("技能对象缺失：%s%s（%s）", row.storage_key, path, exc)
            raise NotFound(f"对象存储里找不到 {path}（存储可能被清理过）") from exc

    async def version_for_view(
        self, *, slug: str | None = None, version: int | None = None, upload_id: UUID | None = None
    ) -> SkillVersion:
        if upload_id is not None:
            row = await self.session.get(SkillVersion, upload_id)
            if row is None:
                raise NotFound(f"上传 {upload_id} 不存在")
            return row
        assert slug is not None and version is not None
        return await self._released(slug, version)

    # ================================================================ 内部

    async def _publish(self, row: SkillVersion, *, actor: str) -> None:
        await self._lock_skill(row.slug)
        current = await self.session.scalar(
            select(func.max(SkillVersion.version)).where(SkillVersion.slug == row.slug)
        )
        version = (current or 0) + 1
        dst = published_prefix(row.slug, version)
        src = row.storage_key

        def _copy() -> int:
            residue = self.storage.list_keys(dst)
            if residue:
                logger.warning("清理 %s 下上次发布失败的残留：%d 个对象", dst, len(residue))
                self.storage.delete_tree(dst)
            copied = self.storage.copy_tree(src, dst)
            if copied != row.file_count:
                self.storage.delete_tree(dst)
                msg = f"拷贝到 {dst} 的对象数 {copied} ≠ 包内文件数 {row.file_count}"
                raise RuntimeError(msg)
            return copied

        await asyncio.to_thread(_copy)
        row.version = version
        row.status = "published"
        row.storage_key = dst
        row.published_by = actor
        row.published_at = utcnow()
        audit.record(
            self.session,
            actor=actor,
            action="skill.publish",
            target_kind="skill",
            target_id=row.slug,
            version=version,
            content_hash=row.content_hash,
        )
        await self.session.flush()
        # ★ 草稿最后删：删失败只是留下孤儿对象，不影响已发布的版本
        try:
            await asyncio.to_thread(self.storage.delete_tree, src)
        except Exception:
            logger.warning("删除草稿 %s 失败（已发布，不影响使用）", src, exc_info=True)

    async def _lock_skill(self, slug: str) -> Skill | None:
        return await self.session.scalar(select(Skill).where(Skill.slug == slug).with_for_update())

    async def _lock_version(self, upload_id: UUID) -> SkillVersion:
        row = await self.session.scalar(
            select(SkillVersion).where(SkillVersion.id == upload_id).with_for_update()
        )
        if row is None:
            raise NotFound(f"上传 {upload_id} 不存在")
        return row

    async def _released(self, slug: str, version: int) -> SkillVersion:
        row = await self.session.scalar(
            select(SkillVersion).where(
                SkillVersion.slug == slug,
                SkillVersion.version == version,
                SkillVersion.status.in_(RUNTIME_VISIBLE),
            )
        )
        if row is None:
            raise NotFound(f"技能 {slug} v{version} 不存在")
        return row

    async def _source(self, slug: str) -> str:
        skill = await self.session.get(Skill, slug)
        if skill is None:
            raise NotFound(f"技能 {slug} 不存在")
        return skill.source

    async def _other_descriptions(self, slug: str) -> dict[str, str]:
        rows = (
            await self.session.execute(
                select(SkillVersion.slug, SkillVersion.description, SkillVersion.version)
                .where(SkillVersion.slug != slug, SkillVersion.status == "published")
                .order_by(SkillVersion.slug, SkillVersion.version)
            )
        ).all()
        return {r.slug: r.description for r in rows}  # 同名后者覆盖前者 = 取最新版


def _runtime_out(row: SkillVersion, source: str) -> SkillVersionOut:
    return SkillVersionOut(
        slug=row.slug,
        version=row.version or 0,
        status=row.status,  # type: ignore[arg-type]
        description=row.description,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        file_count=row.file_count,
        has_scripts=row.has_scripts,
        files=row.files,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        scan_result=row.scan_result,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        published_by=row.published_by,
        published_at=row.published_at,
        status_reason=row.status_reason,
    )


def _admin_out(row: SkillVersion) -> SkillVersionAdminOut:
    return SkillVersionAdminOut.model_validate(row, from_attributes=True)


def _detail_out(skill: Skill, rows: list[SkillVersion]) -> SkillDetailOut:
    published = [r.version for r in rows if r.status == "published" and r.version]
    return SkillDetailOut(
        slug=skill.slug,
        source=skill.source,  # type: ignore[arg-type]
        origin_url=skill.origin_url,
        created_by=skill.created_by,
        created_at=skill.created_at,
        latest=max(published) if published else None,
        versions=[_admin_out(r) for r in rows],
    )
