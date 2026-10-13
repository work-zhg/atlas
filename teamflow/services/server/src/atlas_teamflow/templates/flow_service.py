"""流程模板（流程模板设计）：基本信息、草稿（自动保存 / 接管 / 放弃）、校验、发布、版本。

★ 已发布版本不可变；每个模板同一时间最多一份草稿；草稿记录基线版本，他人先发布后要求重新编辑。
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record
from ..db.models import (
    FileTemplate,
    FlowRole,
    FlowTemplate,
    FlowTemplateDraft,
    FlowTemplateVersion,
    Project,
    TfUser,
)
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound
from ..ids import uuid7
from . import structure

__all__ = ["FlowTemplateService"]


def _text(
    value: str | None, field: str, label: str, max_len: int, required: bool = True
) -> str | None:
    v = (value or "").strip()
    if not v:
        if required:
            raise Invalid("VALIDATION_FAILED", f"请填写{label}", field=field)
        return None
    if len(v) > max_len:
        raise Invalid("VALIDATION_FAILED", f"{label}最多 {max_len} 个字符", field=field)
    return v


class FlowTemplateService:
    def __init__(self, session: AsyncSession, actor: UUID | None) -> None:
        self.session = session
        self.actor = actor

    # ───────────────────────────── 读取

    async def get(self, uuid: UUID) -> FlowTemplate:
        t = (
            await self.session.execute(select(FlowTemplate).where(FlowTemplate.uuid == uuid))
        ).scalar_one_or_none()
        if t is None:
            raise NotFound("FLOW_TEMPLATE_NOT_FOUND", "流程模板不存在")
        return t

    async def list(self, *, status: str | None = None) -> list[FlowTemplate]:
        stmt = select(FlowTemplate).order_by(FlowTemplate.builtin.desc(), FlowTemplate.id)
        if status:
            stmt = stmt.where(FlowTemplate.status == status)
        return list(await self.session.scalars(stmt))

    async def current_version(self, t: FlowTemplate) -> FlowTemplateVersion | None:
        if t.current_version_uuid is None:
            return None
        return (
            await self.session.execute(
                select(FlowTemplateVersion).where(
                    FlowTemplateVersion.uuid == t.current_version_uuid
                )
            )
        ).scalar_one()

    async def versions(self, template_uuid: UUID) -> list[FlowTemplateVersion]:
        return list(
            await self.session.scalars(
                select(FlowTemplateVersion)
                .where(FlowTemplateVersion.template_uuid == template_uuid)
                .order_by(FlowTemplateVersion.major.desc(), FlowTemplateVersion.minor.desc())
            )
        )

    async def version(self, template_uuid: UUID, label: str) -> FlowTemplateVersion:
        try:
            major, minor = (int(x) for x in label.lstrip("v").split("."))
        except ValueError as exc:
            raise NotFound("VERSION_NOT_FOUND", "版本不存在") from exc
        v = (
            await self.session.execute(
                select(FlowTemplateVersion).where(
                    FlowTemplateVersion.template_uuid == template_uuid,
                    FlowTemplateVersion.major == major,
                    FlowTemplateVersion.minor == minor,
                )
            )
        ).scalar_one_or_none()
        if v is None:
            raise NotFound("VERSION_NOT_FOUND", "版本不存在")
        return v

    async def bound_projects(self, template_uuid: UUID) -> list[Project]:
        return list(
            await self.session.scalars(
                select(Project)
                .where(Project.flow_template_uuid == template_uuid)
                .order_by(Project.id)
            )
        )

    async def draft(self, template_uuid: UUID) -> FlowTemplateDraft | None:
        return (
            await self.session.execute(
                select(FlowTemplateDraft).where(FlowTemplateDraft.template_uuid == template_uuid)
            )
        ).scalar_one_or_none()

    async def editor_name(self, uuid: UUID | None) -> str | None:
        if uuid is None:
            return None
        return await self.session.scalar(select(TfUser.name).where(TfUser.uuid == uuid))

    async def file_template_index(self) -> dict[str, dict[str, Any]]:
        return {
            str(f.uuid): {
                "name": f.name,
                "status": f.status,
                "has_version": f.current_version_uuid is not None,
            }
            for f in await self.session.scalars(select(FileTemplate))
        }

    # ───────────────────────────── 基本信息

    async def _check_name(self, name: str, exclude: UUID | None = None) -> None:
        stmt = select(FlowTemplate.uuid).where(FlowTemplate.name == name)
        if exclude:
            stmt = stmt.where(FlowTemplate.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "流程模板名称已存在")

    async def create(self, data: dict[str, Any], copy_from: UUID | None = None) -> FlowTemplate:
        name = _text(data.get("name"), "name", "名称", 64)
        await self._check_name(name)  # type: ignore[arg-type]
        t = FlowTemplate(
            name=name,
            icon=_text(data.get("icon"), "icon", "图标", 16, required=False) or "🧭",
            description=_text(data.get("description"), "description", "说明", 2000, required=False),
            scope=_text(data.get("scope"), "scope", "适用范围", 200, required=False),
            created_by=self.actor,
            updated_by=self.actor,
        )
        self.session.add(t)
        await self.session.flush()
        definition = structure.new_definition()
        base = None
        if copy_from:
            src = await self.get(copy_from)
            cur = await self.current_version(src)
            if cur is not None:
                definition = copy.deepcopy(cur.definition)
        # 新模板直接进入草稿：发布后才有 v1.0
        self.session.add(
            FlowTemplateDraft(
                template_uuid=t.uuid,
                base_version_uuid=base,
                definition=definition,
                editor_uuid=self.actor,
            )
        )
        record(
            self.session,
            self.actor,
            "flow_template.create",
            "flow_template",
            t.uuid,
            name,
            copy_from=copy_from,
        )
        await self.session.flush()
        return t

    async def update(self, uuid: UUID, data: dict[str, Any], version: int | None) -> FlowTemplate:
        t = await self.get(uuid)
        if version is not None and version != t.version:
            raise Conflict("STALE_ROW_VERSION", "该模板已被他人修改，请刷新后重试")
        if "name" in data:
            name = _text(data["name"], "name", "名称", 64)
            await self._check_name(name, exclude=t.uuid)  # type: ignore[arg-type]
            t.name = name  # type: ignore[assignment]
        for key, label, n in (
            ("icon", "图标", 16),
            ("description", "说明", 2000),
            ("scope", "适用范围", 200),
        ):
            if key in data:
                setattr(t, key, _text(data[key], key, label, n, required=False))
        t.version += 1
        t.updated_by = self.actor
        record(self.session, self.actor, "flow_template.update", "flow_template", t.uuid, t.name)
        await self.session.flush()
        return t

    async def set_status(self, uuid: UUID, active: bool) -> FlowTemplate:
        t = await self.get(uuid)
        status = "active" if active else "disabled"
        if t.status != status:
            t.status = status
            t.version += 1
            record(
                self.session,
                self.actor,
                f"flow_template.{'enable' if active else 'disable'}",
                "flow_template",
                t.uuid,
                t.name,
            )
        await self.session.flush()
        return t

    async def delete(self, uuid: UUID) -> None:
        t = await self.get(uuid)
        if await self.session.scalar(
            select(Project.uuid).where(Project.flow_template_uuid == t.uuid).limit(1)
        ):
            raise Conflict("TEMPLATE_IN_USE", "该模板被项目绑定过，不能删除（可以停用）")
        await self.session.execute(
            FlowTemplateDraft.__table__.delete().where(FlowTemplateDraft.template_uuid == t.uuid)
        )
        await self.session.execute(
            FlowTemplateVersion.__table__.delete().where(
                FlowTemplateVersion.template_uuid == t.uuid
            )
        )
        await self.session.delete(t)
        record(self.session, self.actor, "flow_template.delete", "flow_template", t.uuid, t.name)
        await self.session.flush()

    # ───────────────────────────── 草稿

    async def open_draft(self, uuid: UUID) -> FlowTemplateDraft:
        """基于当前版本创建草稿；已存在则返回现有草稿（由调用方看 editor 决定是否接管）。"""
        t = await self.get(uuid)
        d = await self.draft(t.uuid)
        if d is not None:
            return d
        cur = await self.current_version(t)
        # ★ 并发打开（两人同时点「编辑」、前端重复请求）只会有一份草稿：冲突即放弃插入，读回已有的
        await self.session.execute(
            pg_insert(FlowTemplateDraft)
            .values(
                uuid=uuid7(),
                template_uuid=t.uuid,
                base_version_uuid=cur.uuid if cur else None,
                definition=copy.deepcopy(cur.definition) if cur else structure.new_definition(),
                editor_uuid=self.actor,
                updated_at=utcnow(),
                version=1,
            )
            .on_conflict_do_nothing(index_elements=["template_uuid"])
        )
        return await self._draft_or_404(t.uuid)

    async def _draft_or_404(self, uuid: UUID) -> FlowTemplateDraft:
        d = await self.draft(uuid)
        if d is None:
            raise NotFound("DRAFT_NOT_FOUND", "没有草稿")
        return d

    async def save_draft(
        self, uuid: UUID, definition: dict[str, Any], version: int
    ) -> tuple[FlowTemplateDraft, list[Any], list[Any]]:
        d = await self._draft_or_404(uuid)
        if d.editor_uuid and d.editor_uuid != self.actor:
            raise Conflict(
                "DRAFT_LOCKED",
                f"{await self.editor_name(d.editor_uuid)} 正在编辑草稿，可接管后再编辑",
            )
        if version != d.version:
            raise Conflict("STALE_ROW_VERSION", "草稿已被修改，请刷新后重试")
        if (
            not isinstance(definition, dict)
            or not isinstance(definition.get("flow"), list)
            or not isinstance(definition.get("nodes"), dict)
        ):
            raise Invalid("VALIDATION_FAILED", "定义格式不正确")
        norm = structure.normalize(definition)
        if not structure.flow_node_ids(norm["flow"]):
            raise Invalid("VALIDATION_FAILED", "流程至少保留 1 个节点")
        d.definition = norm
        d.editor_uuid = self.actor
        d.version += 1
        d.updated_at = utcnow()
        await self.session.flush()
        errors, warnings = structure.validate(norm, await self.file_template_index())
        return d, errors, warnings

    async def takeover(self, uuid: UUID) -> FlowTemplateDraft:
        d = await self._draft_or_404(uuid)
        prev = d.editor_uuid
        d.editor_uuid = self.actor
        d.version += 1
        record(
            self.session,
            self.actor,
            "flow_template.draft_takeover",
            "flow_template",
            uuid,
            None,
            previous_editor=prev,
        )
        await self.session.flush()
        return d

    async def discard(self, uuid: UUID) -> None:
        d = await self._draft_or_404(uuid)
        t = await self.get(uuid)
        if t.current_version_uuid is None:
            raise Conflict(
                "CANNOT_DISCARD", "模板还没有发布过任何版本，不能放弃草稿（可以删除模板）"
            )
        await self.session.delete(d)
        await self.session.flush()

    async def check(self, uuid: UUID) -> dict[str, Any]:
        """完整校验：错误、警告、影响范围（新增角色涉及的项目）。"""
        t = await self.get(uuid)
        d = await self._draft_or_404(uuid)
        errors, warnings = structure.validate(d.definition, await self.file_template_index())
        cur = await self.current_version(t)
        roles = structure.derive_roles(d.definition)
        new_roles: list[str] = []
        if cur is not None:
            old = set(cur.roles.get("exec", [])) | set(cur.roles.get("review", []))
            new_roles = [r for r in roles["exec"] + roles["review"] if r not in old]
            projects = await self.bound_projects(t.uuid)
            if new_roles and projects:
                warnings.append(
                    {
                        "message": f"本次发布新增了角色（{'、'.join(new_roles)}）："
                        f"绑定该模板的 {len(projects)} 个项目需补充分配",
                        "node": None,
                    }
                )
        if cur is not None and d.base_version_uuid != cur.uuid:
            errors.append(
                {
                    "message": "他人已发布了新版本，需要基于新版本重新编辑（放弃草稿后再编辑）",
                    "node": None,
                    "field": None,
                }
            )
        return {"errors": errors, "warnings": warnings, "roles": roles, "new_roles": new_roles}

    async def publish(
        self, uuid: UUID, change_note: str, acknowledged_warnings: bool
    ) -> FlowTemplateVersion:
        t = await self.get(uuid)
        d = await self._draft_or_404(uuid)
        note = _text(change_note, "change_note", "更新说明", 500)
        cur = await self.current_version(t)
        if cur is not None and d.base_version_uuid != cur.uuid:
            raise Conflict("BASE_VERSION_OUTDATED", "他人已发布了新版本，需要基于新版本重新编辑")
        result = await self.check(uuid)
        if result["errors"]:
            raise Invalid("VALIDATION_FAILED", "发布校验未通过", errors=result["errors"])
        if result["warnings"] and not acknowledged_warnings:
            raise Invalid(
                "WARNINGS_NOT_ACKNOWLEDGED", "有警告需要确认后再发布", warnings=result["warnings"]
            )
        major, minor = (cur.major, cur.minor + 1) if cur else (1, 0)
        v = FlowTemplateVersion(
            template_uuid=t.uuid,
            major=major,
            minor=minor,
            definition=copy.deepcopy(d.definition),
            roles=result["roles"],
            change_note=note,  # type: ignore[arg-type]
            published_by=self.actor,
        )
        self.session.add(v)
        await self.session.flush()
        t.current_version_uuid = v.uuid
        t.version += 1
        t.updated_by = self.actor
        await self.session.delete(d)
        # 新出现的角色自动进入流程角色库
        existing = set(await self.session.scalars(select(FlowRole.name)))
        for r in result["roles"]["exec"] + result["roles"]["review"]:
            if r not in existing:
                self.session.add(FlowRole(name=r[:32], created_by=self.actor))
                existing.add(r)
        record(
            self.session,
            self.actor,
            "flow_template.publish",
            "flow_template",
            t.uuid,
            t.name,
            version=f"v{major}.{minor}",
            note=note,
        )
        await self.session.flush()
        return v

    # ───────────────────────────── 流程角色库

    async def roles(self) -> list[FlowRole]:
        return list(await self.session.scalars(select(FlowRole).order_by(FlowRole.id)))

    async def add_role(self, name: str, description: str | None) -> FlowRole:
        name = _text(name, "name", "角色名称", 32)  # type: ignore[assignment]
        if await self.session.scalar(select(FlowRole.uuid).where(FlowRole.name == name)):
            raise Conflict("NAME_CONFLICT", "角色已存在")
        r = FlowRole(
            name=name,
            description=_text(description, "description", "说明", 200, required=False),
            created_by=self.actor,
        )
        self.session.add(r)
        await self.session.flush()
        return r

    async def counts(self, t: FlowTemplate) -> dict[str, Any]:
        cur = await self.current_version(t)
        projects = int(
            await self.session.scalar(
                select(func.count())
                .select_from(Project)
                .where(Project.flow_template_uuid == t.uuid)
            )
            or 0
        )
        return {
            "node_count": len(structure.flow_node_ids(cur.definition["flow"])) if cur else 0,
            "project_count": projects,
        }
