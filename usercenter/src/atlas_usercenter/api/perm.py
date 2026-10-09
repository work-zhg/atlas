"""权限管理接口：角色授权、岗位、数据授权。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..common import Page
from ..errors import Forbidden
from ..perm.data import DataService
from ..perm.service import GrantService, PositionService
from .deps import Principal, PrincipalDep, SessionDep, require

router = APIRouter(prefix="/api/v1", tags=["perm"])


class SubjectRef(BaseModel):
    type: Literal["user", "dept"]
    id: UUID


class GrantCreate(BaseModel):
    kind: Literal["role", "position"]
    target_ids: list[UUID]
    subjects: list[SubjectRef]
    include_sub: bool = True


class GrantPatch(BaseModel):
    include_sub: bool


class PositionCreate(BaseModel):
    code: str
    name: str
    role_ids: list[UUID] = []


class PositionUpdate(BaseModel):
    name: str | None = None
    role_ids: list[UUID] | None = None
    version: int | None = None


class AclCreate(BaseModel):
    level: Literal["READ", "WRITE", "OWNER"]
    subjects: list[SubjectRef]
    include_sub: bool = True


class AclPatch(BaseModel):
    level: Literal["READ", "WRITE", "OWNER"] | None = None
    include_sub: bool | None = None


# ───────────────────────────── 角色授权


@router.get("/grants")
async def list_grants(
    session: SessionDep,
    kind: str | None = None,
    subject_type: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    p: Principal = require("grant:view"),
) -> dict[str, Any]:
    return await GrantService(session, p.actor).list(
        kind=kind, subject_type=subject_type, q=q, page=Page(page, size)
    )


@router.get("/subjects/{subject_type}/{subject_id}/grants")
async def subject_grants(
    subject_type: str, subject_id: UUID, session: SessionDep, p: Principal = require("grant:view")
) -> dict[str, Any]:
    return await GrantService(session, p.actor).subject_grants(subject_type, subject_id)


@router.post("/grants", status_code=201)
async def add_grants(
    body: GrantCreate, session: SessionDep, p: Principal = require("grant:manage")
) -> dict[str, int]:
    return await GrantService(session, p.actor).add(
        body.kind, body.target_ids, [(s.type, s.id) for s in body.subjects], body.include_sub
    )


@router.patch("/grants/{grant_id}")
async def patch_grant(
    grant_id: UUID, body: GrantPatch, session: SessionDep, p: Principal = require("grant:manage")
) -> dict[str, bool]:
    await GrantService(session, p.actor).set_include_sub(grant_id, body.include_sub)
    return {"ok": True}


@router.delete("/grants/{grant_id}")
async def revoke_grant(
    grant_id: UUID,
    session: SessionDep,
    dry_run: bool = False,
    p: Principal = require("grant:manage"),
) -> dict[str, Any]:
    return await GrantService(session, p.actor).revoke(grant_id, dry_run=dry_run)


@router.get("/roles/{role_id}/grants")
async def role_grants(
    role_id: UUID, session: SessionDep, p: Principal = require("role:view")
) -> dict[str, Any]:
    return await GrantService(session, p.actor).role_grants(role_id)


@router.get("/roles/{role_id}/holders")
async def role_holders(
    role_id: UUID, session: SessionDep, p: Principal = require("role:view")
) -> list[dict[str, Any]]:
    return await GrantService(session, p.actor).holders(role_id)


# ───────────────────────────── 岗位


@router.get("/positions")
async def list_positions(session: SessionDep, p: PrincipalDep) -> list[dict[str, Any]]:
    # 授权弹窗选岗位也需要列表：position:view 或 grant:view 都可以看
    if not (p.has("position:view") or p.has("grant:view")):
        raise Forbidden("PERMISSION_DENIED", "缺少操作权限：position:view")
    return await PositionService(session, p.actor).list()


@router.post("/positions", status_code=201)
async def create_position(
    body: PositionCreate, session: SessionDep, p: Principal = require("position:manage")
) -> dict[str, str]:
    pos = await PositionService(session, p.actor).create(body.code, body.name, body.role_ids)
    return {"id": str(pos.uuid)}


@router.patch("/positions/{position_id}")
async def update_position(
    position_id: UUID,
    body: PositionUpdate,
    session: SessionDep,
    p: Principal = require("position:manage"),
) -> dict[str, bool]:
    data = body.model_dump(exclude_unset=True)
    version = data.pop("version", None)
    if "role_ids" in data:
        data["role_uuids"] = data.pop("role_ids") or []
    await PositionService(session, p.actor).update(position_id, data, version)
    return {"ok": True}


@router.delete("/positions/{position_id}")
async def delete_position(
    position_id: UUID, session: SessionDep, p: Principal = require("position:manage")
) -> dict[str, bool]:
    await PositionService(session, p.actor).delete(position_id)
    return {"ok": True}


# ───────────────────────────── 数据授权（Owner 规则在服务层）


@router.get("/data")
async def list_data(
    session: SessionDep,
    p: PrincipalDep,
    scope: str = "mine",
    data_code: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return await DataService(session, p.actor).list(
        p.user,
        scope=scope,
        data_code=data_code,
        q=q,
        page=Page(page, size),
        can_view_all=p.has("data:view"),
    )


@router.get("/data/{data_id}")
async def data_detail(data_id: UUID, session: SessionDep, p: PrincipalDep) -> dict[str, Any]:
    return await DataService(session, p.actor).detail(
        p.user, data_id, can_view_all=p.has("data:view")
    )


@router.post("/data/{data_id}/acl", status_code=201)
async def add_acl(
    data_id: UUID, body: AclCreate, session: SessionDep, p: PrincipalDep
) -> dict[str, int]:
    return await DataService(session, p.actor).add_acl(
        p.user, data_id, body.level, [(s.type, s.id) for s in body.subjects], body.include_sub
    )


@router.patch("/data-acl/{acl_id}")
async def patch_acl(
    acl_id: UUID, body: AclPatch, session: SessionDep, p: PrincipalDep
) -> dict[str, bool]:
    await DataService(session, p.actor).update_acl(
        p.user, acl_id, level=body.level, include_sub=body.include_sub
    )
    return {"ok": True}


@router.delete("/data-acl/{acl_id}")
async def delete_acl(acl_id: UUID, session: SessionDep, p: PrincipalDep) -> dict[str, bool]:
    await DataService(session, p.actor).delete_acl(p.user, acl_id)
    return {"ok": True}
