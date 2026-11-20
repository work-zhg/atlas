"""技能市场（用户 2026-11-19 定）：产品自带的技能（浏览器、computer use……）不属于任何租户。

    发布   运营中心经运营接口上传，发布即生效（运营中心就是审核人），版本号在市场里递增
    安装   租户从市场拷一个版本进来：对象拷到 {租户}/_skills/{slug}/{version}/，在租户里登记成
           来源 market 的已发布版本，版本号与市场一致 —— 系统参数里写的「browser@3」在每个装了
           v3 的租户里都指同一份内容
    默认   市场里标了「默认安装」的，创建租户时（运营中心开通租户时调运营接口）拷最新的已发布版本
    下架   市场里下架只是不能再装；已经装进租户的是租户自己的了，要停用由租户自己停

★ 拷贝而不是引用：装进来以后就是租户的技能（审计、停用都在租户里），市场后来改了不会悄悄改变
  租户正在用的内容；运行时也只认租户里的技能，不跨租户读。
★ 市场的 slug 装进租户后就被它占用：租户自己有同名技能（来源不是 market）时安装报冲突；
  来源是 market 的技能不能在租户里上传新版本（service.ingest），新版本从市场装。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..db.models import MarketSkill, MarketSkillVersion, Skill, SkillVersion
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..schemas import MarketInstalled, MarketSkillOut, MarketVersionOut
from ..settings import SkillSettings
from ..storage import SkillStorage, market_prefix, published_prefix
from .package import RawPackage
from .scanner import content_scan, parse

logger = logging.getLogger(__name__)

__all__ = ["MarketService", "parse_ref"]


def parse_ref(ref: str) -> tuple[str, int | None]:
    """「slug」或「slug@版本」。"""
    slug, _, version = ref.strip().partition("@")
    if not slug:
        raise Invalid(f"技能写法不对：{ref!r}（slug 或 slug@版本）")
    if not version:
        return slug, None
    try:
        return slug, int(version.removeprefix("v"))
    except ValueError as exc:
        raise Invalid(f"技能写法不对：{ref!r}（版本要是数字）") from exc


@dataclass
class MarketService:
    session: AsyncSession
    storage: SkillStorage
    settings: SkillSettings

    # ================================================================ 市场（运营接口）

    async def publish(
        self, pkg: RawPackage, *, actor: str, install_by_default: bool | None = None
    ) -> MarketSkillVersion:
        """上传一个新版本，直接发布。内容扫描照做、结果记下来（运营中心上传前自己看过）。"""
        parsed = parse(pkg, description_max=self.settings.skill_description_max_chars)
        skill = await self.session.scalar(
            select(MarketSkill).where(MarketSkill.slug == parsed.slug).with_for_update()
        )
        if skill is None:
            skill = MarketSkill(
                slug=parsed.slug, install_by_default=bool(install_by_default), created_by=actor
            )
            self.session.add(skill)
            await self.session.flush()
        elif install_by_default is not None:
            skill.install_by_default = install_by_default
        same = await self.session.scalar(
            select(MarketSkillVersion.version).where(
                MarketSkillVersion.slug == parsed.slug,
                MarketSkillVersion.content_hash == parsed.content_hash,
            )
        )
        if same is not None:
            raise Conflict(f"内容与市场里的 {parsed.slug} v{same} 完全相同，不需要发新版本")
        current = await self.session.scalar(
            select(func.max(MarketSkillVersion.version)).where(
                MarketSkillVersion.slug == parsed.slug
            )
        )
        version = (current or 0) + 1
        prefix = market_prefix(parsed.slug, version)

        def _put() -> None:
            if self.storage.list_keys(prefix):  # 上次发布失败的残留
                self.storage.delete_tree(prefix)
            self.storage.put_tree(prefix, pkg.files)

        await asyncio.to_thread(_put)
        now = utcnow()
        row = MarketSkillVersion(
            slug=parsed.slug,
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
                "review": {"result": "market", "by": actor, "at": now.isoformat()},
            },
            published_by=actor,
            published_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def set_default(self, slug: str, install_by_default: bool) -> MarketSkill:
        skill = await self.session.scalar(select(MarketSkill).where(MarketSkill.slug == slug))
        if skill is None:
            raise NotFound(f"技能市场里没有 {slug}")
        skill.install_by_default = install_by_default
        await self.session.flush()
        return skill

    async def revoke(self, slug: str, version: int, *, reason: str) -> MarketSkillVersion:
        if not reason:
            raise Invalid("下架必须写明原因")
        row = await self._version(slug, version)
        row.status, row.status_reason = "revoked", reason
        await self.session.flush()
        return row

    async def listing(self) -> list[MarketSkillOut]:
        """市场里的全部技能和版本（不分租户；租户视角的「已安装」由 for_tenant 补上）。"""
        skills = (await self.session.scalars(select(MarketSkill).order_by(MarketSkill.slug))).all()
        rows = (
            await self.session.scalars(
                select(MarketSkillVersion).order_by(
                    MarketSkillVersion.slug, MarketSkillVersion.version
                )
            )
        ).all()
        by_slug: dict[str, list[MarketSkillVersion]] = {}
        for row in rows:
            by_slug.setdefault(row.slug, []).append(row)
        out = []
        for skill in skills:
            versions = by_slug.get(skill.slug, [])
            latest = next((v for v in reversed(versions) if v.status == "published"), None)
            out.append(
                MarketSkillOut(
                    slug=skill.slug,
                    install_by_default=skill.install_by_default,
                    latest=latest.version if latest else None,
                    description=latest.description if latest else None,
                    versions=[_version_out(v) for v in versions],
                )
            )
        return out

    # ================================================================ 租户（在租户的上下文里调）

    async def for_tenant(self) -> list[MarketSkillOut]:
        """市场里还能装的（已发布）技能，带上本租户已经装了哪些版本。"""
        installed: dict[str, list[int]] = {}
        rows = await self.session.execute(
            select(SkillVersion.slug, SkillVersion.version)
            .join(Skill, Skill.slug == SkillVersion.slug)
            .where(Skill.source == "market", SkillVersion.version.is_not(None))
        )
        for slug, version in rows:
            installed.setdefault(slug, []).append(version)
        out = []
        for item in await self.listing():
            published = [v for v in item.versions if v.status == "published"]
            if not published:
                continue
            mine = sorted(installed.get(item.slug, []))
            out.append(item.model_copy(update={"versions": published, "installed": mine}))
        return out

    async def install(self, slug: str, version: int | None, *, actor: str) -> MarketInstalled:
        """把市场里的一个版本拷进当前租户。幂等：装过就直接返回。"""
        market = await self._version(slug, version) if version else await self._latest(slug)
        if market.status != "published":
            raise Conflict(f"技能市场里的 {slug} v{market.version} 已下架，不能安装")
        skill = await self.session.scalar(
            select(Skill).where(Skill.slug == slug).with_for_update()
        )
        if skill is not None and skill.source != "market":
            raise Conflict(f"本租户已有同名技能 {slug}（来源 {skill.source}），不能从技能市场安装")
        existing = await self.session.scalar(
            select(SkillVersion).where(
                SkillVersion.slug == slug, SkillVersion.version == market.version
            )
        )
        if existing is not None:
            return MarketInstalled(slug=slug, version=market.version, copied=False)
        if skill is None:
            self.session.add(Skill(slug=slug, source="market", created_by=actor))
            await self.session.flush()
        dst = published_prefix(slug, market.version)

        def _copy() -> int:
            if self.storage.list_keys(dst):  # 上次安装失败的残留
                self.storage.delete_tree(dst)
            copied = self.storage.copy_tree(market.storage_key, dst)
            if copied != market.file_count:
                self.storage.delete_tree(dst)
                msg = f"拷贝到 {dst} 的对象数 {copied} ≠ 包内文件数 {market.file_count}"
                raise RuntimeError(msg)
            return copied

        await asyncio.to_thread(_copy)
        now = utcnow()
        self.session.add(
            SkillVersion(
                slug=slug,
                version=market.version,
                status="published",
                description=market.description,
                frontmatter=market.frontmatter,
                storage_key=dst,
                content_hash=market.content_hash,
                size_bytes=market.size_bytes,
                file_count=market.file_count,
                has_scripts=market.has_scripts,
                files=market.files,
                scan_result={**market.scan_result, "history": []},
                uploaded_by=actor,
                reviewed_by=market.published_by,
                reviewed_at=market.published_at,
                review_note="从技能市场安装",
                published_by=actor,
                published_at=now,
            )
        )
        audit.record(
            self.session,
            actor=actor,
            action="skill.install",
            target_kind="skill",
            target_id=slug,
            version=market.version,
            content_hash=market.content_hash,
        )
        await self.session.flush()
        return MarketInstalled(slug=slug, version=market.version, copied=True)

    async def install_many(self, refs: list[str], *, actor: str) -> list[MarketInstalled]:
        """refs 为空 = 装全部「默认安装」的（各自最新的已发布版本）。"""
        if refs:
            wanted = [parse_ref(r) for r in refs]
        else:
            defaults = await self.session.scalars(
                select(MarketSkill.slug).where(MarketSkill.install_by_default.is_(True))
            )
            wanted = [(slug, None) for slug in defaults]
        out = []
        for slug, version in wanted:
            if version is None and not await self._has_published(slug):
                logger.warning("技能市场里的 %s 没有已发布的版本，跳过", slug)
                continue
            out.append(await self.install(slug, version, actor=actor))
        return out

    # ================================================================ 内部

    async def _version(self, slug: str, version: int) -> MarketSkillVersion:
        row = await self.session.scalar(
            select(MarketSkillVersion).where(
                MarketSkillVersion.slug == slug, MarketSkillVersion.version == version
            )
        )
        if row is None:
            raise NotFound(f"技能市场里没有 {slug} v{version}")
        return row

    async def _latest(self, slug: str) -> MarketSkillVersion:
        row = await self.session.scalar(
            select(MarketSkillVersion)
            .where(MarketSkillVersion.slug == slug, MarketSkillVersion.status == "published")
            .order_by(MarketSkillVersion.version.desc())
            .limit(1)
        )
        if row is None:
            raise NotFound(f"技能市场里没有已发布的 {slug}")
        return row

    async def _has_published(self, slug: str) -> bool:
        return (
            await self.session.scalar(
                select(MarketSkillVersion.id).where(
                    MarketSkillVersion.slug == slug, MarketSkillVersion.status == "published"
                )
            )
        ) is not None


def _version_out(row: MarketSkillVersion) -> MarketVersionOut:
    return MarketVersionOut.model_validate(row, from_attributes=True)
