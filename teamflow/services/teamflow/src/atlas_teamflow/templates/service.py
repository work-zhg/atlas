"""TemplateService：流程模板的唯一入口（c4-model/teamflow/code-templates.html）。

    新建        空白新建，或从流程市场拷贝某一版（记来源；拷过来就是团队自己的）
    编排        一份草稿，整份结构保存，rev 是乐观锁
    校验、发布  有错误不能发布；版本号递增；发布后不改，进行中的流程各自锁在发起时的版本上
    读版本      给项目算配齐、给流程运行发起时锁定

★ 角色是模板自己的概念（结构里的角色表），和用户中心的系统角色没有关系。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import structure
from ..audit import record
from ..errors import Conflict, Invalid, NotFound
from ..market.service import MarketService
from .models import FlowTemplate, TemplateDraft, TemplateVersion

__all__ = ["TemplateService"]


class TemplateService:
    def __init__(self, session: AsyncSession, market: MarketService) -> None:
        self.session = session
        self.market = market

    async def get(self, template_uuid: UUID) -> FlowTemplate:
        t = await self.session.scalar(
            select(FlowTemplate).where(FlowTemplate.uuid == template_uuid)
        )
        if t is None:
            raise NotFound("TEMPLATE_NOT_FOUND", "流程模板不存在")
        return t

    async def of_team(
        self, team_uuid: UUID, *, include_archived: bool = False
    ) -> list[FlowTemplate]:
        stmt = (
            select(FlowTemplate)
            .where(FlowTemplate.team_uuid == team_uuid)
            .order_by(FlowTemplate.id)
        )
        if not include_archived:
            stmt = stmt.where(FlowTemplate.status == "active")
        return list(await self.session.scalars(stmt))

    async def _free_name(self, name: str) -> str:
        """租户内不重名：同名时加「（2）」「（3）」…"""
        base, n, candidate = name[:58], 1, name
        while await self.session.scalar(
            select(FlowTemplate.uuid).where(FlowTemplate.name == candidate)
        ):
            n += 1
            candidate = f"{base}（{n}）"
        return candidate

    async def _check_name(self, name: str, exclude: UUID | None = None) -> str:
        name = (name or "").strip()
        if not name or len(name) > 64:
            raise Invalid("VALIDATION_FAILED", "模板名称必填，最多 64 个字")
        stmt = select(FlowTemplate.uuid).where(FlowTemplate.name == name)
        if exclude:
            stmt = stmt.where(FlowTemplate.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "模板名称已存在")
        return name

    # ───────────────────────────── 新建

    async def create_blank(self, team_uuid: UUID, name: str, *, actor: UUID) -> FlowTemplate:
        t = FlowTemplate(
            team_uuid=team_uuid,
            name=await self._check_name(name),
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(t)
        await self.session.flush()
        self.session.add(
            TemplateDraft(template_uuid=t.uuid, structure=structure.new_structure(), editor=actor)
        )
        record(self.session, actor, "template.create", "flow_template", t.uuid, t.name)
        await self.session.flush()
        return t

    async def copy_from_market(
        self, team_uuid: UUID, market_uuid: UUID, *, actor: UUID
    ) -> FlowTemplate:
        """拷市场模板的最新发布版：写成本团队的新模板和它的草稿，记来源；要团队自己发布才能绑项目。"""
        pkg = await self.market.export(market_uuid)
        t = FlowTemplate(
            team_uuid=team_uuid,
            name=await self._free_name(pkg.name),
            description=pkg.description or None,
            origin_market_uuid=pkg.template_uuid,
            origin_market_version=pkg.version,
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(t)
        await self.session.flush()
        self.session.add(TemplateDraft(template_uuid=t.uuid, structure=pkg.structure, editor=actor))
        await self.market.record_copy(
            pkg, tenant=TenantScope.require(), team=team_uuid, actor=actor
        )
        record(
            self.session,
            actor,
            "template.copy",
            "flow_template",
            t.uuid,
            t.name,
            market=pkg.template_uuid,
            version=pkg.version,
        )
        await self.session.flush()
        return t

    async def update(
        self, template_uuid: UUID, meta: dict[str, Any], *, actor: UUID
    ) -> FlowTemplate:
        t = await self.get(template_uuid)
        if "name" in meta and meta["name"] != t.name:
            t.name = await self._check_name(meta["name"], exclude=t.uuid)
        if "description" in meta:
            t.description = (meta["description"] or "").strip() or None
        if "icon" in meta:
            t.icon = meta["icon"] or None
        if "archived" in meta:
            t.status = "archived" if meta["archived"] else "active"
        t.version += 1
        t.updated_by = actor
        record(self.session, actor, "template.update", "flow_template", t.uuid, t.name)
        await self.session.flush()
        return t

    # ───────────────────────────── 编排

    async def draft(self, template_uuid: UUID) -> TemplateDraft:
        d = await self.session.scalar(
            select(TemplateDraft).where(TemplateDraft.template_uuid == template_uuid)
        )
        if d is None:
            raise NotFound("DRAFT_NOT_FOUND", "模板草稿不存在")
        return d

    async def save_draft(
        self, template_uuid: UUID, raw: dict[str, Any], *, rev: int, actor: UUID
    ) -> TemplateDraft:
        await self.get(template_uuid)
        d = await self.draft(template_uuid)
        if rev != d.rev:
            raise Conflict("DRAFT_CONFLICT", "草稿已被别人改过，请刷新后再改")
        s = structure.normalize(raw)
        ids = structure.node_ids(s)
        if len(ids) != len(set(ids)):
            raise Invalid("STRUCTURE_INVALID", "节点 id 重复")
        d.structure = s
        d.rev += 1
        d.editor = actor
        await self.session.flush()
        return d

    async def validate(self, template_uuid: UUID) -> tuple[list[Any], list[Any]]:
        return structure.validate((await self.draft(template_uuid)).structure)

    async def publish(
        self, template_uuid: UUID, note: str | None, *, actor: UUID
    ) -> TemplateVersion:
        t = await self.get(template_uuid)
        d = await self.draft(template_uuid)
        errors, _ = structure.validate(d.structure)
        if errors:
            raise Invalid("TEMPLATE_INVALID", "模板还有错误，不能发布", errors=errors)
        # 锁模板行：两个人同时发布时版本号不撞
        await self.session.execute(
            select(FlowTemplate.id).where(FlowTemplate.uuid == t.uuid).with_for_update()
        )
        t.latest_version_no += 1
        v = TemplateVersion(
            template_uuid=t.uuid,
            version_no=t.latest_version_no,
            structure=d.structure,
            note=(note or "").strip()[:500] or None,
            published_by=actor,
        )
        self.session.add(v)
        d.base_version_no = t.latest_version_no
        record(
            self.session,
            actor,
            "template.publish",
            "flow_template",
            t.uuid,
            t.name,
            version=v.version_no,
        )
        await self.session.flush()
        return v

    # ───────────────────────────── 读版本

    async def latest_published(self, template_uuid: UUID) -> TemplateVersion | None:
        t = await self.get(template_uuid)
        if t.latest_version_no == 0:
            return None
        return await self.session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_uuid == t.uuid,
                TemplateVersion.version_no == t.latest_version_no,
            )
        )

    async def version(self, version_uuid: UUID) -> TemplateVersion:
        v = await self.session.scalar(
            select(TemplateVersion).where(TemplateVersion.uuid == version_uuid)
        )
        if v is None:
            raise NotFound("TEMPLATE_VERSION_NOT_FOUND", "模板版本不存在")
        return v

    async def versions(self, template_uuid: UUID) -> list[TemplateVersion]:
        return list(
            await self.session.scalars(
                select(TemplateVersion)
                .where(TemplateVersion.template_uuid == template_uuid)
                .order_by(TemplateVersion.version_no.desc())
            )
        )
