"""SkillService：技能版本的唯一入口（c4-model/skill/code-versions.html）。接口和命令行都调它。

    写   上传（ingest）、审查（review）、停用 / 启用 / 下架（change_status）、重新检查（rescan）、
         登记已有技能（adopt）
    读   给运行时的目录（只有已发布、停用、下架的）；给管理页的列表、详情、包内文件

存储与数据库的先后顺序是刻意安排的：

  上传   先写草稿对象，再插行。插行失败 → 草稿区留下孤儿对象（无害，可清理）；
         反过来的话会出现「有行无对象」，审查人打开就是 404。
  发布   在技能行锁内分配版本号 → 拷贝并核对 → 改行；**提交之后**再删草稿（commit()）。
         被拒的上传也是提交之后再删草稿。

★ 写操作和审计在同一个事务里；调用方用 commit() 提交（接口在响应前提交）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..check.package import Limits, RawPackage
from ..check.scanner import content_scan, parse, similarity_scan
from ..db.models import Skill, SkillVersion
from ..db.types import utcnow, uuid7
from ..errors import Conflict, Invalid, NotFound
from ..schemas import (
    RevokedSkill,
    SkillCatalogItem,
    SkillDetailOut,
    SkillVersionAdminOut,
    SkillVersionBrief,
    SkillVersionOut,
)
from ..settings import SkillSettings
from ..store.objects import SkillStorage
from ..store.store import SkillStore
from .pipeline import PublishPipeline
from .policy import ReviewPolicy
from .repository import SkillRepository

logger = logging.getLogger(__name__)

__all__ = ["MarketPackage", "SkillService", "limits_of"]

#: 在线查看文件的上限
FILE_VIEW_MAX_BYTES = 1024 * 1024


def limits_of(settings: SkillSettings) -> Limits:
    return Limits(
        max_bytes=settings.skill_max_bytes,
        max_files=settings.skill_max_files,
        max_file_bytes=settings.skill_max_file_bytes,
    )


@dataclass(frozen=True)
class MarketPackage:
    """技能市场里的一个已发布版本，装进租户时交给技能版本（市场的表结构它不认识）。"""

    slug: str
    version: int
    storage_key: str
    description: str
    frontmatter: dict[str, Any]
    content_hash: str
    size_bytes: int
    file_count: int
    has_scripts: bool
    files: list[dict[str, Any]]
    scan_result: dict[str, Any]
    published_by: str
    published_at: datetime


@dataclass
class SkillService:
    session: AsyncSession
    storage: SkillStorage
    settings: SkillSettings
    #: 提交之后要删的草稿
    _drafts_to_delete: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.store = SkillStore(self.storage)
        self.repo = SkillRepository(self.session)
        self.policy = ReviewPolicy(allow_self_review=self.settings.allow_self_review)
        self.pipeline = PublishPipeline(self.repo, self.store)

    async def commit(self) -> None:
        """提交，然后删掉这次发布、被拒的草稿（删失败只留下孤儿对象）。"""
        await self.session.commit()
        drafts, self._drafts_to_delete = self._drafts_to_delete, []
        for prefix in drafts:
            await self.store.delete(prefix)

    # ================================================================ 写

    async def ingest(
        self,
        pkg: RawPackage,
        *,
        source: str,
        actor: str,
        origin_url: str | None = None,
    ) -> SkillVersion:
        """上传一个新版本，进入本租户的待审查。

        第 1 层（归档）已在读包时做完；这里做第 2、3 层与描述冲突检查。
        """
        self.policy.check_source(source, origin_url)
        parsed = parse(pkg, description_max=self.settings.skill_description_max_chars)
        skill = await self.repo.lock_skill(parsed.slug)
        if skill is None:
            skill = Skill(
                uuid=uuid7(),
                slug=parsed.slug,
                source=source,
                origin_url=origin_url,
                created_by=actor,
            )
            self.session.add(skill)
            await self._flush(f"技能 {parsed.slug} 正在被别人上传，稍后再试")
        else:
            self.policy.check_new_version(skill, source)
            in_flight = await self.repo.pending_of(skill)
            if in_flight is not None:
                raise Conflict(f"技能 {parsed.slug} 已有一个待审查的上传（{in_flight}），先处理它")
            same = await self.repo.same_content(skill, parsed.content_hash)
            if same is not None:
                raise Conflict(f"内容与 {parsed.slug} v{same} 完全相同，不需要发新版本")

        others = await self.repo.published_descriptions(except_skill=skill)
        upload_id = uuid7()
        prefix = await self.store.put_draft(str(upload_id), pkg.files)
        row = SkillVersion(
            uuid=upload_id,
            skill=skill,
            skill_uuid=skill.uuid,
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
        # 部分唯一索引兜底：同一技能同时只能有一个待审查的上传
        await self._flush(f"技能 {parsed.slug} 已有一个待审查的上传，先处理它")
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
        return row

    async def _flush(self, conflict: str) -> None:
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise Conflict(conflict) from exc

    async def review(
        self, upload_id: UUID, *, reviewer: str, approve: bool, note: str
    ) -> SkillVersion:
        row = await self.repo.by_upload(upload_id, lock=True)
        if row.status != "pending_review":
            raise Conflict(f"该上传当前状态是 {row.status}，不在待审查中")
        self.policy.check_reviewer(reviewer, row.uploaded_by)
        draft = row.storage_key
        row.review(reviewer, note, approve=approve)
        audit.record(
            self.session,
            actor=reviewer,
            action="skill.approve" if approve else "skill.reject",
            target_kind="skill",
            target_id=row.slug,
            upload_id=str(row.uuid),
            note=note,
        )
        if approve:
            draft = await self.pipeline.publish(row, actor=reviewer)
        else:
            await self.session.flush()
        self._drafts_to_delete.append(draft)
        return row

    async def change_status(
        self, slug: str, version: int, *, action: str, actor: str, reason: str | None
    ) -> SkillVersion:
        """停用 / 启用 / 紧急下架。"""
        row = await self.repo.version(slug, version, lock=True)
        row.change_status(action, reason)
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
        """把源仓库里**已有**的包登记进当前租户（多租户之前手工放进桶根 _skills/ 的技能）。

        只读对象、算哈希，不移动对象 —— 已经引用它的 agent 不受任何影响。
        """
        try:
            await self.repo.version(slug, version)
        except NotFound:
            pass
        else:
            raise Conflict(f"{slug} v{version} 已经登记过")
        prefix, files = await self.store.read_legacy(slug, version)
        if not files:
            raise NotFound(f"源仓库里没有 {prefix}")
        pkg = RawPackage(files=files)
        parsed = parse(pkg, description_max=self.settings.skill_description_max_chars)
        if parsed.slug != slug:
            raise Invalid(f"SKILL.md 里的 name 是 {parsed.slug!r}，与目录名 {slug!r} 不一致")
        skill = await self.repo.lock_skill(slug)
        if skill is None:
            skill = Skill(uuid=uuid7(), slug=slug, source="builtin", created_by=actor)
            self.session.add(skill)
        now = utcnow()
        row = SkillVersion(
            skill=skill,
            skill_uuid=skill.uuid,
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

    async def install_from_market(self, pkg: MarketPackage, *, actor: str) -> bool:
        """把技能市场里的一个版本装成本租户来源 market 的已发布版本（版本号与市场一致）。

        幂等：装过这个版本就什么都不做，→ False。运营中心就是审核人，不再审查。
        """
        skill = await self.repo.lock_skill(pkg.slug)
        if skill is not None and skill.source != "market":
            raise Conflict(
                f"本租户已有同名技能 {pkg.slug}（来源 {skill.source}），不能从技能市场安装"
            )
        if skill is None:
            skill = Skill(uuid=uuid7(), slug=pkg.slug, source="market", created_by=actor)
            self.session.add(skill)
            await self._flush(f"技能 {pkg.slug} 正在被别人安装，稍后再试")
        else:
            try:
                await self.repo.version(pkg.slug, pkg.version)
            except NotFound:
                pass
            else:
                return False
        # 拷到本租户的发布区并核对（同审查通过的发布）
        dst = await self.store.publish(pkg.storage_key, pkg.slug, pkg.version, pkg.file_count)
        self.session.add(
            SkillVersion(
                skill=skill,
                skill_uuid=skill.uuid,
                version=pkg.version,
                status="published",
                description=pkg.description,
                frontmatter=pkg.frontmatter,
                storage_key=dst,
                content_hash=pkg.content_hash,
                size_bytes=pkg.size_bytes,
                file_count=pkg.file_count,
                has_scripts=pkg.has_scripts,
                files=pkg.files,
                scan_result={**pkg.scan_result, "history": []},
                uploaded_by=actor,
                reviewed_by=pkg.published_by,
                reviewed_at=pkg.published_at,
                review_note="从技能市场安装",
                published_by=actor,
                published_at=utcnow(),
            )
        )
        audit.record(
            self.session,
            actor=actor,
            action="skill.install",
            target_kind="skill",
            target_id=pkg.slug,
            version=pkg.version,
            content_hash=pkg.content_hash,
        )
        await self.session.flush()
        return True

    async def installed_from_market(self) -> dict[str, list[int]]:
        """本租户从技能市场装了哪些技能的哪些版本。"""
        out: dict[str, list[int]] = {}
        for skill in await self.repo.skills():
            if skill.source != "market":
                continue
            rows = await self.repo.versions(skill)
            out[skill.slug] = sorted(r.version for r in rows if r.version is not None)
        return out

    async def rescan(self, slug: str, version: int, *, actor: str) -> SkillVersion:
        """规则升级后重扫存量版本：结果只追加到 history，不改版本内容。"""
        row = await self.repo.released(slug, version)
        result = content_scan(RawPackage(files=await self.store.read_tree(row.storage_key)))
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
        by_skill = _group(await self.repo.versions(runtime=True))
        items = []
        for skill in await self.repo.skills():
            versions = sorted(by_skill.get(skill.uuid, []), key=lambda v: v.version or 0)
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
        skill = await self._skill(slug)
        rows = await self.repo.versions(skill, runtime=True)
        return [_runtime_out(r) for r in sorted(rows, key=lambda v: v.version or 0)]

    async def runtime_version(self, slug: str, version: int) -> SkillVersionOut:
        return _runtime_out(await self.repo.released(slug, version))

    async def revoked(self) -> list[RevokedSkill]:
        return [
            RevokedSkill(
                slug=r.slug, version=r.version or 0, reason=r.status_reason, at=r.updated_at
            )
            for r in await self.repo.revoked()
        ]

    # ================================================================ 读 · 管理

    async def admin_list(self) -> list[SkillDetailOut]:
        by_skill = _group(await self.repo.versions())
        return [_detail_out(s, by_skill.get(s.uuid, [])) for s in await self.repo.skills()]

    async def admin_detail(self, slug: str) -> SkillDetailOut:
        skill = await self._skill(slug)
        return _detail_out(skill, await self.repo.versions(skill))

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
            return await self.store.read(row.storage_key, path)
        except Exception as exc:
            # ★ 库里有记录、桶里没对象：存储被清过或换了桶。给 404 而不是 500 ——
            #   500 不带 CORS 头，浏览器只会报一句看不出原因的 Failed to fetch。
            logger.warning("技能对象缺失：%s%s（%s）", row.storage_key, path, exc)
            raise NotFound(f"对象存储里找不到 {path}（存储可能被清理过）") from exc

    async def version_for_view(
        self, *, slug: str | None = None, version: int | None = None, upload_id: UUID | None = None
    ) -> SkillVersion:
        if upload_id is not None:
            return await self.repo.by_upload(upload_id)
        assert slug is not None and version is not None
        return await self.repo.released(slug, version)

    async def _skill(self, slug: str) -> Skill:
        skill = await self.repo.skill(slug)
        if skill is None:
            raise NotFound(f"技能 {slug} 不存在")
        return skill


def _group(rows: list[SkillVersion]) -> dict[UUID, list[SkillVersion]]:
    out: dict[UUID, list[SkillVersion]] = {}
    for row in rows:
        out.setdefault(row.skill_uuid, []).append(row)
    return out


def _runtime_out(row: SkillVersion) -> SkillVersionOut:
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
        source=row.skill.source,  # type: ignore[arg-type]
        scan_result=row.scan_result,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        published_by=row.published_by,
        published_at=row.published_at,
        status_reason=row.status_reason,
        storage_key=row.storage_key,
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
