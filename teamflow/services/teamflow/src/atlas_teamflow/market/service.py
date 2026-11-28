"""MarketService：流程市场的唯一入口（c4-model/teamflow/code-market.html）。

    浏览      按分类列上架的、看详情（谁都能看）
    维护      新建、存草稿、发布、上下架、拷贝统计（运营管理员：系统租户 + 流程市场读写，入口判）
    给团队    导出某一版的结构、记一笔拷贝（流程模板领域 copy_from_market 调）

★ 不属于任何租户：表不按租户过滤；拷贝记录带租户和团队，只用来统计。
★ 拷贝而不是引用：市场之后发新版本、下架都不影响已经拷走的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import structure
from ..errors import Conflict, Invalid, NotFound
from .models import CATEGORIES, MarketCopy, MarketTemplate, MarketVersion

__all__ = ["MarketPackage", "MarketService"]


@dataclass(frozen=True)
class MarketPackage:
    template_uuid: UUID
    version: int
    name: str
    description: str
    structure: dict[str, Any]


class MarketService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, template_uuid: UUID) -> MarketTemplate:
        t = await self.session.scalar(
            select(MarketTemplate).where(MarketTemplate.uuid == template_uuid)
        )
        if t is None:
            raise NotFound("MARKET_TEMPLATE_NOT_FOUND", "市场模板不存在")
        return t

    # ───────────────────────────── 浏览

    async def listing(
        self, category: str | None = None, *, maintainer: bool = False
    ) -> list[MarketTemplate]:
        """普通人只看上架的；运营管理员看全部（含草稿、下架）。"""
        stmt = select(MarketTemplate).order_by(MarketTemplate.id)
        if not maintainer:
            stmt = stmt.where(MarketTemplate.status == "listed")
        if category:
            stmt = stmt.where(MarketTemplate.category == category)
        return list(await self.session.scalars(stmt))

    async def latest(self, t: MarketTemplate) -> MarketVersion | None:
        if t.latest_version == 0:
            return None
        return await self.session.scalar(
            select(MarketVersion).where(
                MarketVersion.market_template_uuid == t.uuid,
                MarketVersion.version == t.latest_version,
            )
        )

    async def detail(
        self, template_uuid: UUID, *, maintainer: bool = False
    ) -> tuple[MarketTemplate, MarketVersion | None]:
        t = await self.get(template_uuid)
        if t.status != "listed" and not maintainer:
            raise NotFound("MARKET_TEMPLATE_NOT_FOUND", "市场模板不存在")
        return t, await self.latest(t)

    # ───────────────────────────── 维护

    async def create(self, name: str, category: str, *, actor: UUID) -> MarketTemplate:
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise Invalid("VALIDATION_FAILED", "名称必填，最多 64 个字")
        if category not in CATEGORIES:
            raise Invalid("VALIDATION_FAILED", "分类不认识")
        if await self.session.scalar(
            select(MarketTemplate.uuid).where(MarketTemplate.name == name)
        ):
            raise Conflict("NAME_CONFLICT", "市场里已经有同名的模板")
        t = MarketTemplate(
            name=name,
            category=category,
            description="",
            draft=structure.new_structure(),
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(t)
        await self.session.flush()
        return t

    async def save_draft(
        self,
        template_uuid: UUID,
        draft: dict[str, Any] | None,
        meta: dict[str, Any],
        *,
        actor: UUID,
    ) -> MarketTemplate:
        t = await self.get(template_uuid)
        if draft is not None:
            t.draft = structure.normalize(draft)
        for key in ("description", "who", "icon", "color"):
            if key in meta:
                setattr(t, key, meta[key] if key == "description" else (meta[key] or None))
        if "category" in meta:
            if meta["category"] not in CATEGORIES:
                raise Invalid("VALIDATION_FAILED", "分类不认识")
            t.category = meta["category"]
        if "name" in meta and meta["name"] != t.name:
            name = (meta["name"] or "").strip()
            if not name or len(name) > 64:
                raise Invalid("VALIDATION_FAILED", "名称必填，最多 64 个字")
            if await self.session.scalar(
                select(MarketTemplate.uuid).where(
                    MarketTemplate.name == name, MarketTemplate.uuid != t.uuid
                )
            ):
                raise Conflict("NAME_CONFLICT", "市场里已经有同名的模板")
            t.name = name
        t.version += 1
        t.updated_by = actor
        await self.session.flush()
        return t

    async def publish(self, template_uuid: UUID, note: str | None, *, actor: UUID) -> MarketVersion:
        """发布前用流程结构校验，有错误不能发布；第一次发布即上架。"""
        t = await self.get(template_uuid)
        errors, _ = structure.validate(t.draft)
        if errors:
            raise Invalid("TEMPLATE_INVALID", "模板还有错误，不能发布", errors=errors)
        t.latest_version += 1
        v = MarketVersion(
            market_template_uuid=t.uuid,
            version=t.latest_version,
            structure=t.draft,
            note=(note or "").strip()[:500] or None,
            published_by=actor,
        )
        self.session.add(v)
        if t.status == "draft":
            t.status = "listed"
        t.updated_by = actor
        await self.session.flush()
        return v

    async def set_listed(self, template_uuid: UUID, on: bool, *, actor: UUID) -> MarketTemplate:
        t = await self.get(template_uuid)
        if t.latest_version == 0:
            raise Conflict("NOT_PUBLISHED", "还没发布过，不能上下架")
        t.status = "listed" if on else "unlisted"
        t.updated_by = actor
        await self.session.flush()
        return t

    async def stats(self, template_uuid: UUID) -> tuple[int, int]:
        """（拷贝次数，租户数）。"""
        row = (
            await self.session.execute(
                select(func.count(), func.count(func.distinct(MarketCopy.tenant_uuid))).where(
                    MarketCopy.market_template_uuid == template_uuid
                )
            )
        ).one()
        return int(row[0]), int(row[1])

    # ───────────────────────────── 给团队拷贝

    async def export(self, template_uuid: UUID) -> MarketPackage:
        """只能拷上架模板的最新发布版。"""
        t = await self.get(template_uuid)
        if t.status != "listed":
            raise Conflict("MARKET_TEMPLATE_UNLISTED", "这个模板已经下架，不能再拷贝")
        v = await self.latest(t)
        assert v is not None
        return MarketPackage(t.uuid, v.version, t.name, t.description, v.structure)

    async def record_copy(
        self, pkg: MarketPackage, *, tenant: UUID, team: UUID, actor: UUID
    ) -> None:
        self.session.add(
            MarketCopy(
                market_template_uuid=pkg.template_uuid,
                version=pkg.version,
                tenant_uuid=tenant,
                team_uuid=team,
                copied_by=actor,
            )
        )
