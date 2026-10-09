"""流程模板：查看（flow_template:view，平台管理员与团队管理员），维护（flow_template:manage，平台管理员）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from ..identity import Principal
from ..templates import structure
from ..templates.flow_service import FlowTemplateService
from .deps import SessionDep, require
from .views import flow_template_view, flow_version_view, iso, project_view, sid, user_names

router = APIRouter(prefix="/api/v1/flow-templates", tags=["flow-templates"])

VIEW = require("flow_template:view")
MANAGE = require("flow_template:manage")


class FlowTemplateBody(BaseModel):
    name: str | None = None
    icon: str | None = None
    description: str | None = None
    scope: str | None = None
    copy_from: UUID | None = None
    version: int | None = None


class DraftBody(BaseModel):
    definition: dict[str, Any]
    version: int


class PublishBody(BaseModel):
    change_note: str
    acknowledged_warnings: bool = False


class RoleBody(BaseModel):
    name: str
    description: str | None = None


async def _draft_view(svc: FlowTemplateService, uuid: UUID) -> dict[str, Any] | None:
    d = await svc.draft(uuid)
    if d is None:
        return None
    errors, warnings = structure.validate(d.definition, await svc.file_template_index())
    return {
        "definition": d.definition,
        "deps": structure.derive_deps(d.definition["flow"]),
        "base_version_id": sid(d.base_version_uuid),
        "editor_id": sid(d.editor_uuid),
        "editor_name": await svc.editor_name(d.editor_uuid),
        "editing_by_me": d.editor_uuid == svc.actor,
        "updated_at": iso(d.updated_at),
        "version": d.version,
        "errors": errors,
        "warnings": warnings,
    }


async def _detail(svc: FlowTemplateService, uuid: UUID, *, with_draft: bool) -> dict[str, Any]:
    t = await svc.get(uuid)
    cur = await svc.current_version(t)
    versions = await svc.versions(t.uuid)
    names = await user_names(svc.session, {v.published_by for v in versions})
    out = flow_template_view(t, cur, **await svc.counts(t))
    out["definition"] = cur.definition if cur else None
    out["deps"] = structure.derive_deps(cur.definition["flow"]) if cur else {}
    out["versions"] = [
        {**flow_version_view(v), "published_by_name": names.get(str(v.published_by))}
        for v in versions
    ]
    out["projects"] = [project_view(p) for p in await svc.bound_projects(t.uuid)]
    d = await svc.draft(t.uuid)
    out["has_draft"] = d is not None
    out["draft_editor_name"] = await svc.editor_name(d.editor_uuid) if d else None
    if with_draft:
        out["draft"] = await _draft_view(svc, t.uuid)
    return out


@router.get("")
async def list_templates(
    session: SessionDep, p: Principal = VIEW, status: str | None = None
) -> list[dict[str, Any]]:
    svc = FlowTemplateService(session, p.uuid)
    out = []
    for t in await svc.list(status=status):
        d = await svc.draft(t.uuid)
        out.append(
            {
                **flow_template_view(t, await svc.current_version(t), **await svc.counts(t)),
                "has_draft": d is not None,
            }
        )
    return out


@router.post("", status_code=201)
async def create(
    body: FlowTemplateBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any]:
    svc = FlowTemplateService(session, p.uuid)
    data = body.model_dump(exclude_unset=True)
    t = await svc.create(data, copy_from=data.pop("copy_from", None))
    return await _detail(svc, t.uuid, with_draft=True)


@router.get("/roles")
async def roles(session: SessionDep, p: Principal = VIEW) -> list[dict[str, Any]]:
    return [
        {"id": str(r.uuid), "name": r.name, "description": r.description}
        for r in await FlowTemplateService(session, p.uuid).roles()
    ]


@router.post("/roles", status_code=201)
async def add_role(body: RoleBody, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    r = await FlowTemplateService(session, p.uuid).add_role(body.name, body.description)
    return {"id": str(r.uuid), "name": r.name, "description": r.description}


@router.get("/{tid}")
async def detail(tid: UUID, session: SessionDep, p: Principal = VIEW) -> dict[str, Any]:
    return await _detail(
        FlowTemplateService(session, p.uuid), tid, with_draft=p.has("flow_template:manage")
    )


@router.patch("/{tid}")
async def update(
    tid: UUID, body: FlowTemplateBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any]:
    svc = FlowTemplateService(session, p.uuid)
    data = body.model_dump(exclude_unset=True, exclude={"copy_from"})
    version = data.pop("version", None)
    await svc.update(tid, data, version)
    return await _detail(svc, tid, with_draft=True)


@router.post("/{tid}/enable")
async def enable(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    svc = FlowTemplateService(session, p.uuid)
    await svc.set_status(tid, True)
    return await _detail(svc, tid, with_draft=True)


@router.post("/{tid}/disable")
async def disable(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    svc = FlowTemplateService(session, p.uuid)
    await svc.set_status(tid, False)
    return await _detail(svc, tid, with_draft=True)


@router.delete("/{tid}")
async def delete(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, bool]:
    await FlowTemplateService(session, p.uuid).delete(tid)
    return {"ok": True}


@router.get("/{tid}/versions/{label}")
async def version(
    tid: UUID, label: str, session: SessionDep, p: Principal = VIEW
) -> dict[str, Any]:
    v = await FlowTemplateService(session, p.uuid).version(tid, label)
    return {
        **flow_version_view(v, definition=True),
        "deps": structure.derive_deps(v.definition["flow"]),
    }


# ───────────────────────────── 草稿


@router.post("/{tid}/draft")
async def open_draft(
    tid: UUID, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any] | None:
    svc = FlowTemplateService(session, p.uuid)
    await svc.open_draft(tid)
    return await _draft_view(svc, tid)


@router.put("/{tid}/draft")
async def save_draft(
    tid: UUID, body: DraftBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any] | None:
    svc = FlowTemplateService(session, p.uuid)
    await svc.save_draft(tid, body.definition, body.version)
    return await _draft_view(svc, tid)


@router.post("/{tid}/draft/takeover")
async def takeover(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any] | None:
    svc = FlowTemplateService(session, p.uuid)
    await svc.takeover(tid)
    return await _draft_view(svc, tid)


@router.delete("/{tid}/draft")
async def discard(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, bool]:
    await FlowTemplateService(session, p.uuid).discard(tid)
    return {"ok": True}


@router.post("/{tid}/draft/check")
async def check(tid: UUID, session: SessionDep, p: Principal = MANAGE) -> dict[str, Any]:
    return await FlowTemplateService(session, p.uuid).check(tid)


@router.post("/{tid}/publish")
async def publish(
    tid: UUID, body: PublishBody, session: SessionDep, p: Principal = MANAGE
) -> dict[str, Any]:
    svc = FlowTemplateService(session, p.uuid)
    await svc.publish(tid, body.change_note, body.acknowledged_warnings)
    return await _detail(svc, tid, with_draft=True)
