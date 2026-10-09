"""对外视图：API 里的 id 一律为 uuid。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db.models import (
    FileTemplate,
    FileTemplateVersion,
    FlowTemplate,
    FlowTemplateVersion,
    Project,
    Team,
    TeamAgent,
)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def sid(v: Any) -> str | None:
    return str(v) if v is not None else None


def file_template_view(t: FileTemplate, cur: FileTemplateVersion | None = None) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "name": t.name,
        "icon": t.icon,
        "description": t.description,
        "usage": t.usage,
        "status": t.status,
        "version": t.version,
        "current_version": file_version_view(cur) if cur else None,
        "updated_at": iso(t.updated_at),
    }


def file_version_view(v: FileTemplateVersion, *, content: bool = False) -> dict[str, Any]:
    out = {
        "id": str(v.uuid),
        "version_no": v.version_no,
        "file_name": v.file_name,
        "size_bytes": v.size_bytes,
        "sha256": v.sha256,
        "change_note": v.change_note,
        "uploaded_by": sid(v.uploaded_by),
        "uploaded_at": iso(v.uploaded_at),
    }
    if content:
        out["content"] = v.content
    return out


def flow_version_view(v: FlowTemplateVersion, *, definition: bool = False) -> dict[str, Any]:
    out = {
        "id": str(v.uuid),
        "label": f"v{v.major}.{v.minor}",
        "roles": v.roles,
        "change_note": v.change_note,
        "published_by": sid(v.published_by),
        "published_at": iso(v.published_at),
    }
    if definition:
        out["definition"] = v.definition
    return out


def flow_template_view(
    t: FlowTemplate, cur: FlowTemplateVersion | None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "name": t.name,
        "icon": t.icon,
        "description": t.description,
        "scope": t.scope,
        "builtin": t.builtin,
        "status": t.status,
        "version": t.version,
        "current_version": flow_version_view(cur) if cur else None,
        "updated_at": iso(t.updated_at),
        **extra,
    }


def team_view(t: Team, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(t.uuid),
        "name": t.name,
        "description": t.description,
        "status": t.status,
        "version": t.version,
        "created_at": iso(t.created_at),
        **extra,
    }


def agent_view(a: TeamAgent, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(a.uuid),
        "atlas_agent_id": a.atlas_agent_id,
        "name": a.name,
        "avatar_key": a.avatar_key,
        "description": a.description,
        "model": a.model,
        "available": a.available,
        "synced_at": iso(a.synced_at),
        **extra,
    }


def project_view(p: Project, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(p.uuid),
        "team_id": str(p.team_uuid),
        "name": p.name,
        "description": p.description,
        "flow_template_id": sid(p.flow_template_uuid),
        "status": p.status,
        "version": p.version,
        "updated_at": iso(p.updated_at),
        **extra,
    }


async def user_names(session: Any, ids: set[Any]) -> dict[str, str]:
    """uuid → 姓名（本地用户缓存）。"""
    from sqlalchemy import select

    from ..db.models import TfUser

    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        str(u.uuid): u.name
        for u in await session.scalars(select(TfUser).where(TfUser.uuid.in_(ids)))
    }
