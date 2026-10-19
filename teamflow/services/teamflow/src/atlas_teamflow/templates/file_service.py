"""文件模板（文件模板设计）。

只支持 Markdown；≤ 100 KB；合法 UTF-8（去 BOM、拒 NUL）；版本不可变；上传即生效；
与生效版本内容相同则拒绝；停用后不能上传新版本；被引用过的模板不能删除。
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import record
from ..db.models import (
    FileTemplate,
    FileTemplateVersion,
    FlowTemplate,
    FlowTemplateDraft,
    FlowTemplateVersion,
)
from ..errors import Conflict, Invalid, NotFound

__all__ = ["FileTemplateService", "parse_markdown_upload"]

USAGES = ("artifact", "review_rule", "other")


def parse_markdown_upload(file_name: str, raw: bytes, max_bytes: int) -> tuple[str, str]:
    """校验上传文件 → (统一后的文件名, 文本)。"""
    name = (file_name or "").strip() or "template.md"
    lower = name.lower()
    if not (lower.endswith(".md") or lower.endswith(".markdown")):
        raise Invalid("UNSUPPORTED_FILE_TYPE", "只支持 Markdown 文件（.md）")
    if lower.endswith(".markdown"):
        name = name[: -len(".markdown")] + ".md"
    if len(raw) > max_bytes:
        raise Invalid("FILE_TOO_LARGE", f"文件超过 {max_bytes // 1024} KB")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if b"\x00" in raw:
        raise Invalid("INVALID_ENCODING", "文件包含二进制内容，不是文本文件")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Invalid("INVALID_ENCODING", "文件不是合法的 UTF-8 文本") from exc
    if not text.strip():
        raise Invalid("EMPTY_CONTENT", "文件为空或只有空白")
    return name, text


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


class FileTemplateService:
    def __init__(self, session: AsyncSession, actor: UUID | None) -> None:
        self.session = session
        self.actor = actor

    async def get(self, uuid: UUID) -> FileTemplate:
        t = (
            await self.session.execute(select(FileTemplate).where(FileTemplate.uuid == uuid))
        ).scalar_one_or_none()
        if t is None:
            raise NotFound("FILE_TEMPLATE_NOT_FOUND", "文件模板不存在")
        return t

    async def list(
        self, *, usage: str | None = None, status: str | None = None, q: str | None = None
    ) -> list[FileTemplate]:
        stmt = select(FileTemplate).order_by(FileTemplate.id)
        if usage:
            stmt = stmt.where(FileTemplate.usage == usage)
        if status:
            stmt = stmt.where(FileTemplate.status == status)
        if q:
            stmt = stmt.where(FileTemplate.name.like(f"%{q.strip()}%"))
        return list(await self.session.scalars(stmt))

    async def version(
        self, template_uuid: UUID, version_no: int | None = None, version_uuid: UUID | None = None
    ) -> FileTemplateVersion:
        stmt = select(FileTemplateVersion).where(FileTemplateVersion.template_uuid == template_uuid)
        if version_uuid is not None:
            stmt = stmt.where(FileTemplateVersion.uuid == version_uuid)
        else:
            stmt = stmt.where(FileTemplateVersion.version_no == version_no)
        v = (await self.session.execute(stmt)).scalar_one_or_none()
        if v is None:
            raise NotFound("VERSION_NOT_FOUND", "版本不存在")
        return v

    async def current(self, t: FileTemplate) -> FileTemplateVersion | None:
        if t.current_version_uuid is None:
            return None
        return await self.version(t.uuid, version_uuid=t.current_version_uuid)

    async def versions(self, template_uuid: UUID) -> list[FileTemplateVersion]:
        return list(
            await self.session.scalars(
                select(FileTemplateVersion)
                .where(FileTemplateVersion.template_uuid == template_uuid)
                .order_by(FileTemplateVersion.version_no.desc())
            )
        )

    async def references(self, template_uuid: UUID) -> list[dict[str, Any]]:
        """引用它的流程模板节点：当前版本与草稿中的（文件模板设计 §6.3）。"""
        out: list[dict[str, Any]] = []
        key = str(template_uuid)
        rows = (
            await self.session.execute(
                select(FlowTemplate, FlowTemplateVersion).join(
                    FlowTemplateVersion,
                    FlowTemplateVersion.uuid == FlowTemplate.current_version_uuid,
                )
            )
        ).all()
        for tpl, ver in rows:
            for nid, node in (ver.definition.get("nodes") or {}).items():
                if node.get("artifact_file_template_id") == key:
                    out.append(
                        {
                            "flow_template_id": str(tpl.uuid),
                            "flow_template": tpl.name,
                            "node_id": nid,
                            "node": node.get("name"),
                            "in": f"v{ver.major}.{ver.minor}",
                        }
                    )
        drafts = (
            await self.session.execute(
                select(FlowTemplate, FlowTemplateDraft).join(
                    FlowTemplateDraft, FlowTemplateDraft.template_uuid == FlowTemplate.uuid
                )
            )
        ).all()
        for tpl, draft in drafts:
            for nid, node in (draft.definition.get("nodes") or {}).items():
                if node.get("artifact_file_template_id") == key:
                    out.append(
                        {
                            "flow_template_id": str(tpl.uuid),
                            "flow_template": tpl.name,
                            "node_id": nid,
                            "node": node.get("name"),
                            "in": "草稿",
                        }
                    )
        return out

    async def _ever_referenced(self, template_uuid: UUID) -> bool:
        key = str(template_uuid)
        for definition in await self.session.scalars(select(FlowTemplateVersion.definition)):
            if any(
                n.get("artifact_file_template_id") == key
                for n in (definition.get("nodes") or {}).values()
            ):
                return True
        return bool(await self.references(template_uuid))

    async def _check_name(self, name: str, exclude: UUID | None = None) -> None:
        stmt = select(FileTemplate.uuid).where(FileTemplate.name == name)
        if exclude:
            stmt = stmt.where(FileTemplate.uuid != exclude)
        if await self.session.scalar(stmt):
            raise Conflict("NAME_CONFLICT", "文件模板名称已存在")

    async def create(self, data: dict[str, Any]) -> FileTemplate:
        name = _text(data.get("name"), "name", "名称", 64)
        await self._check_name(name)  # type: ignore[arg-type]
        usage = data.get("usage") or "artifact"
        if usage not in USAGES:
            raise Invalid(
                "VALIDATION_FAILED", "用途只能是产物格式 / 评审规则 / 其他", field="usage"
            )
        t = FileTemplate(
            name=name,
            icon=_text(data.get("icon"), "icon", "图标", 16, required=False) or "📄",
            description=_text(data.get("description"), "description", "说明", 2000, required=False),
            usage=usage,
            created_by=self.actor,
            updated_by=self.actor,
        )
        self.session.add(t)
        await self.session.flush()
        record(self.session, self.actor, "file_template.create", "file_template", t.uuid, name)
        return t

    async def update(self, uuid: UUID, data: dict[str, Any], version: int | None) -> FileTemplate:
        t = await self.get(uuid)
        if version is not None and version != t.version:
            raise Conflict("STALE_ROW_VERSION", "该模板已被他人修改，请刷新后重试")
        if "name" in data:
            name = _text(data["name"], "name", "名称", 64)
            await self._check_name(name, exclude=t.uuid)  # type: ignore[arg-type]
            t.name = name  # type: ignore[assignment]
        if "icon" in data:
            t.icon = _text(data["icon"], "icon", "图标", 16, required=False) or "📄"
        if "description" in data:
            t.description = _text(data["description"], "description", "说明", 2000, required=False)
        if "usage" in data:
            if data["usage"] not in USAGES:
                raise Invalid(
                    "VALIDATION_FAILED", "用途只能是产物格式 / 评审规则 / 其他", field="usage"
                )
            t.usage = data["usage"]
        t.version += 1
        t.updated_by = self.actor
        record(self.session, self.actor, "file_template.update", "file_template", t.uuid, t.name)
        await self.session.flush()
        return t

    async def set_status(self, uuid: UUID, active: bool) -> FileTemplate:
        t = await self.get(uuid)
        status = "active" if active else "disabled"
        if t.status != status:
            t.status = status
            t.version += 1
            record(
                self.session,
                self.actor,
                f"file_template.{'enable' if active else 'disable'}",
                "file_template",
                t.uuid,
                t.name,
            )
        await self.session.flush()
        return t

    async def delete(self, uuid: UUID) -> None:
        t = await self.get(uuid)
        if await self._ever_referenced(t.uuid):
            raise Conflict("TEMPLATE_IN_USE", "该模板被流程模板引用过，不能删除（可以停用）")
        await self.session.execute(
            FileTemplateVersion.__table__.delete().where(
                FileTemplateVersion.template_uuid == t.uuid
            )
        )
        await self.session.delete(t)
        record(self.session, self.actor, "file_template.delete", "file_template", t.uuid, t.name)
        await self.session.flush()

    async def upload(
        self,
        uuid: UUID,
        file_name: str,
        raw: bytes,
        change_note: str,
        version: int | None,
        max_bytes: int,
    ) -> FileTemplateVersion:
        t = await self.get(uuid)
        if t.status == "disabled":
            raise Conflict("TEMPLATE_DISABLED", "模板已停用，请先启用再上传")
        if version is not None and version != t.version:
            raise Conflict(
                "STALE_ROW_VERSION", "该模板已被他人修改（可能有人同时上传），请刷新后重试"
            )
        note = _text(change_note, "change_note", "更新说明", 500)
        name, text = parse_markdown_upload(file_name, raw, max_bytes)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cur = await self.current(t)
        if cur is not None and cur.sha256 == digest:
            raise Invalid("CONTENT_UNCHANGED", "与生效版本内容相同，无需上传")
        no = (
            int(
                await self.session.scalar(
                    select(func.coalesce(func.max(FileTemplateVersion.version_no), 0)).where(
                        FileTemplateVersion.template_uuid == t.uuid
                    )
                )
                or 0
            )
            + 1
        )
        v = FileTemplateVersion(
            template_uuid=t.uuid,
            version_no=no,
            file_name=name,
            content=text,
            size_bytes=len(text.encode("utf-8")),
            sha256=digest,
            change_note=note,  # type: ignore[arg-type]
            uploaded_by=self.actor,
        )
        self.session.add(v)
        await self.session.flush()
        t.current_version_uuid = v.uuid
        t.version += 1
        t.updated_by = self.actor
        record(
            self.session,
            self.actor,
            "file_template.upload",
            "file_template",
            t.uuid,
            t.name,
            version=no,
        )
        await self.session.flush()
        return v
