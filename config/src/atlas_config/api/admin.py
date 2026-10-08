"""管理页用的接口（设计 §13.2）。

动作用 `/…/approve` 这种子路径而不是 `…:approve`：Starlette 的路径参数会把
冒号后的部分一起吞进去，写成冒号只会多一层手工解析。
"""

from __future__ import annotations

import mimetypes
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy import select

from ..auth import Principal, require_admin, require_builder
from ..db.models import ConfigAudit
from ..schemas import (
    AuditOut,
    McpServerIn,
    McpServerOut,
    McpServerPatch,
    ReviewDecisionIn,
    SkillDetailOut,
    SkillVersionAdminOut,
    StatusChangeIn,
    ToolReviewIn,
    ToolReviewOut,
)
from ..skill.package import ScanBlocked, load_zip
from ..skill.service import limits_of
from .deps import RegistryDep, SessionDep, SkillServiceDep

router = APIRouter(prefix="/config", tags=["config"])

Builder = Annotated[Principal, Depends(require_builder)]
Admin = Annotated[Principal, Depends(require_admin)]


def _admin_out(row: object) -> SkillVersionAdminOut:
    return SkillVersionAdminOut.model_validate(row, from_attributes=True)


# ───────────────────────────────────────────── 技能 · 读


@router.get("/skills", response_model=list[SkillDetailOut])
async def list_skills(service: SkillServiceDep, _: Builder) -> list[SkillDetailOut]:
    return await service.admin_list()


@router.get("/skills/{slug}", response_model=SkillDetailOut)
async def get_skill(slug: str, service: SkillServiceDep, _: Builder) -> SkillDetailOut:
    return await service.admin_detail(slug)


def _file_response(path: str, body: bytes) -> Response:
    media, _ = mimetypes.guess_type(path)
    if media is None or not (media.startswith("text/") or media in ("application/json",)):
        media = "text/plain"
    return Response(content=body, media_type=f"{media}; charset=utf-8")


@router.get("/skills/{slug}/versions/{version}/files/{path:path}")
async def read_published_file(
    slug: str, version: int, path: str, service: SkillServiceDep, _: Builder
) -> Response:
    row = await service.version_for_view(slug=slug, version=version)
    return _file_response(path, await service.read_file(row, path))


@router.get("/skill-uploads/{upload_id}/files/{path:path}")
async def read_upload_file(
    upload_id: UUID, path: str, service: SkillServiceDep, _: Builder
) -> Response:
    """审查人看待审查的包。"""
    row = await service.version_for_view(upload_id=upload_id)
    return _file_response(path, await service.read_file(row, path))


# ───────────────────────────────────────────── 技能 · 写


@router.post("/skills", response_model=SkillVersionAdminOut, status_code=201)
async def upload_skill(
    service: SkillServiceDep,
    principal: Admin,
    file: Annotated[UploadFile, File(description="技能包 zip")],
    source: Annotated[Literal["builtin", "tenant", "imported"], Form()] = "builtin",
    origin_url: Annotated[str | None, Form()] = None,
) -> SkillVersionAdminOut:
    limits = limits_of(service.settings)
    # ★ 先按上限读：请求体比上限还大就不必整个读进内存
    data = await file.read(limits.max_bytes + 1)
    if len(data) > limits.max_bytes:
        raise ScanBlocked("archive", [f"上传的 zip 超过 {limits.max_bytes} 字节"])
    pkg = load_zip(data, limits)
    row = await service.ingest(pkg, source=source, actor=principal.user, origin_url=origin_url)
    return _admin_out(row)


@router.post("/skill-uploads/{upload_id}/approve", response_model=SkillVersionAdminOut)
async def approve(
    upload_id: UUID, body: ReviewDecisionIn, service: SkillServiceDep, principal: Admin
) -> SkillVersionAdminOut:
    return _admin_out(
        await service.review(upload_id, reviewer=principal.user, approve=True, note=body.note)
    )


@router.post("/skill-uploads/{upload_id}/reject", response_model=SkillVersionAdminOut)
async def reject(
    upload_id: UUID, body: ReviewDecisionIn, service: SkillServiceDep, principal: Admin
) -> SkillVersionAdminOut:
    return _admin_out(
        await service.review(upload_id, reviewer=principal.user, approve=False, note=body.note)
    )


@router.post("/skills/{slug}/versions/{version}/{action}", response_model=SkillVersionAdminOut)
async def change_status(
    slug: str,
    version: int,
    action: Literal["disable", "enable", "revoke", "rescan"],
    service: SkillServiceDep,
    principal: Admin,
    body: StatusChangeIn | None = None,
) -> SkillVersionAdminOut:
    if action == "rescan":
        row = await service.rescan(slug, version, actor=principal.user)
    else:
        row = await service.change_status(
            slug, version, action=action, actor=principal.user, reason=body.reason if body else None
        )
    return _admin_out(row)


# ───────────────────────────────────────────── MCP


@router.get("/mcp/servers", response_model=list[McpServerOut])
async def list_servers(registry: RegistryDep, _: Builder) -> list[McpServerOut]:
    return await registry.list()


@router.post("/mcp/servers", response_model=McpServerOut, status_code=201)
async def create_server(body: McpServerIn, registry: RegistryDep, principal: Admin) -> McpServerOut:
    return await registry.create(body, actor=principal.user)


@router.get("/mcp/servers/{name}", response_model=McpServerOut)
async def get_server(name: str, registry: RegistryDep, _: Builder) -> McpServerOut:
    return await registry.get(name)


@router.patch("/mcp/servers/{name}", response_model=McpServerOut)
async def patch_server(
    name: str, body: McpServerPatch, registry: RegistryDep, principal: Admin
) -> McpServerOut:
    return await registry.patch(name, body, actor=principal.user)


@router.get("/mcp/servers/{name}/reviews", response_model=list[ToolReviewOut])
async def list_reviews(name: str, registry: RegistryDep, _: Builder) -> list[ToolReviewOut]:
    return await registry.reviews(name)


@router.post("/mcp/servers/{name}/reviews", response_model=ToolReviewOut, status_code=201)
async def review_tool(
    name: str, body: ToolReviewIn, registry: RegistryDep, principal: Admin
) -> ToolReviewOut:
    return await registry.review(name, body, actor=principal.user)


# ───────────────────────────────────────────── 审计


@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    session: SessionDep,
    _: Admin,
    target_kind: str | None = None,
    target_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditOut]:
    query = select(ConfigAudit).order_by(ConfigAudit.at.desc()).limit(limit)
    if target_kind:
        query = query.where(ConfigAudit.target_kind == target_kind)
    if target_id:
        query = query.where(ConfigAudit.target_id == target_id)
    rows = (await session.scalars(query)).all()
    return [AuditOut.model_validate(r, from_attributes=True) for r in rows]
