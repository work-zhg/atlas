"""元数据、文档级权限、版本与时效（doc/knowledge-enterprise-design.html §7.2 / §7.4 / §7.5）。

字段定义   知识库级：名称、类型、可选值、是否多值、是否允许查询理解抽取；定义时给所有索引版本
           建 payload 索引
文档赋值   doc_metadata / acl / valid_from / valid_to；改完只按文档更新 payload，不重新向量化
新版本     上传时指定 supersedes：与旧文档同一 version_group；新版本入库完成后旧版本
           is_current = false、valid_to 截止到新版本生效（indexing/runner.py）
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import METADATA_TYPES, Document, MetadataField
from ..errors import Conflict, Invalid, NotFound
from ..indexing.loader import sync_doc_payload
from ..indexing.versions import stores
from ..rag.filters import BUILTIN_FIELDS, FieldDef, field_index_kind, normalize_value
from ..runtime import Runtime
from .permission import DatasetAccess

__all__ = ["MetadataService", "field_def", "fields_of", "normalize_acl"]

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ACL_TAG = re.compile(r"^(?:public|[udrg]:[\w.@-]{1,128})$")


def field_def(row: MetadataField) -> FieldDef:
    return FieldDef(
        name=row.name,
        type=row.type,
        multi=row.multi,
        options=tuple(row.options or ()),
        description=row.description,
        llm_extract=row.llm_extract,
    )


async def fields_of(
    session: AsyncSession, dataset_uuids: list[UUID]
) -> dict[str, dict[str, FieldDef]]:
    """库 uuid → {字段名: 定义}（含内置字段）。"""
    out: dict[str, dict[str, FieldDef]] = {str(u): dict(BUILTIN_FIELDS) for u in dataset_uuids}
    if dataset_uuids:
        rows = await session.scalars(
            select(MetadataField).where(MetadataField.dataset_uuid.in_(dataset_uuids))
        )
        for r in rows.all():
            out[str(r.dataset_uuid)][r.name] = field_def(r)
    return out


def normalize_acl(raw: Any) -> list[str] | None:
    """None / [] / ["public"] = 库内公开（存 None）。标签：u:用户 d:部门 r:角色 g:用户组。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise Invalid("acl 必须是数组")
    tags = list(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))
    bad = [t for t in tags if not _ACL_TAG.match(t)]
    if bad:
        raise Invalid(
            f"权限标签格式不对：{'、'.join(bad[:3])}（应为 u:/d:/r:/g: 加 id，或 public）"
        )
    if not tags or "public" in tags:
        return None
    return tags[:200]


def _when(raw: Any, label: str) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid(f"{label} 格式不对，应为 ISO 8601 时间") from exc
    if dt.tzinfo is None:
        raise Invalid(f"{label} 须带时区")
    return dt


class MetadataService:
    def __init__(self, session: AsyncSession, rt: Runtime) -> None:
        self._s = session
        self._rt = rt

    # ------------------------------------------------------------------ 字段定义

    async def list_fields(self, access: DatasetAccess) -> list[MetadataField]:
        rows = await self._s.scalars(
            select(MetadataField)
            .where(MetadataField.dataset_uuid == access.ds.uuid)
            .order_by(MetadataField.id)
        )
        return list(rows.all())

    async def create_field(self, access: DatasetAccess, body: dict[str, Any]) -> MetadataField:
        access.require_admin()
        name = str(body.get("name") or "").strip()
        if not _NAME.match(name):
            raise Invalid("字段名只能用字母、数字、下划线，且不以数字开头")
        if name in BUILTIN_FIELDS:
            raise Invalid(f"{name} 是内置字段")
        ftype = body.get("type") or "string"
        if ftype not in METADATA_TYPES:
            raise Invalid(f"字段类型只能是 {' / '.join(METADATA_TYPES)}")
        options = [str(x).strip() for x in body.get("options") or [] if str(x).strip()]
        if ftype == "enum" and not options:
            raise Invalid("枚举字段需要可选值")
        exists = await self._s.scalar(
            select(MetadataField.id).where(
                MetadataField.dataset_uuid == access.ds.uuid, MetadataField.name == name
            )
        )
        if exists is not None:
            raise Conflict(f"字段 {name} 已存在")
        row = MetadataField(
            dataset_uuid=access.ds.uuid,
            name=name,
            type=ftype,
            description=str(body.get("description") or ""),
            options=options or None,
            multi=bool(body.get("multi", False)),
            llm_extract=bool(body.get("llm_extract", False)),
        )
        self._s.add(row)
        await self._s.flush()
        for store in await stores(self._s, self._rt, access.ds):
            await store.ensure_index(f"meta.{name}", field_index_kind(field_def(row)))
        return row

    async def delete_field(self, access: DatasetAccess, name: str) -> None:
        """只删定义；文档上已赋的值留着（不再参与过滤），重新定义同名字段即恢复。"""
        access.require_admin()
        res = await self._s.execute(
            delete(MetadataField).where(
                MetadataField.dataset_uuid == access.ds.uuid, MetadataField.name == name
            )
        )
        if not res.rowcount:  # type: ignore[attr-defined]
            raise NotFound("字段不存在")

    # ------------------------------------------------------------------ 文档赋值

    async def normalize_metadata(self, access: DatasetAccess, raw: Any) -> dict[str, Any] | None:
        if raw in (None, {}):
            return None
        if not isinstance(raw, dict):
            raise Invalid("metadata 必须是对象")
        fields = (await fields_of(self._s, [access.ds.uuid]))[str(access.ds.uuid)]
        out: dict[str, Any] = {}
        for k, v in raw.items():
            f = fields.get(k)
            if f is None or k in BUILTIN_FIELDS:
                raise Invalid(f"没有元数据字段：{k}（先在知识库里定义）")
            if v is None:
                continue
            if isinstance(v, list) and not f.multi:
                raise Invalid(f"字段 {k} 不是多值字段")
            out[k] = normalize_value(f, v)
        return out or None

    async def update_document(
        self, access: DatasetAccess, doc: Document, body: dict[str, Any]
    ) -> Document:
        access.require_write()
        if "metadata" in body:
            doc.doc_metadata = await self.normalize_metadata(access, body["metadata"])
        if "acl" in body:
            doc.acl = normalize_acl(body["acl"])
        if "valid_from" in body:
            doc.valid_from = _when(body["valid_from"], "valid_from")
        if "valid_to" in body:
            doc.valid_to = _when(body["valid_to"], "valid_to")
        if doc.valid_from and doc.valid_to and doc.valid_from >= doc.valid_to:
            raise Invalid("生效时间须早于失效时间")
        if "is_current" in body:
            doc.is_current = bool(body["is_current"])
        await self._s.flush()
        await sync_doc_payload(self._s, self._rt, access.ds, doc)
        return doc

    async def link_version(self, access: DatasetAccess, doc: Document, supersedes: UUID) -> None:
        """doc 是 supersedes 的新版本（新版本入库完成后旧版本才被取代）。"""
        old = await self._s.scalar(
            select(Document).where(
                Document.uuid == supersedes, Document.dataset_uuid == access.ds.uuid
            )
        )
        if old is None:
            raise NotFound("要替换的旧版本文档不存在")
        if old.version_group is None:
            old.version_group = old.uuid
        doc.version_group = old.version_group
        # 新版本默认沿用旧版本的元数据与权限（上传时显式给了的除外）
        if doc.doc_metadata is None:
            doc.doc_metadata = old.doc_metadata
        if doc.acl is None:
            doc.acl = old.acl
