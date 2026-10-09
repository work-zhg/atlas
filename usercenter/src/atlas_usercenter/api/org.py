"""组织管理接口。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from ..common import UNSET, Page
from ..db.models import Credential
from ..org.service import OrgService
from ..perm.views import positions_of
from .deps import Principal, SessionDep, require
from .views import user_view

router = APIRouter(prefix="/api/v1/depts", tags=["org"])


class DeptCreate(BaseModel):
    parent_id: UUID
    name: str


class DeptUpdate(BaseModel):
    name: str | None = None
    leader_id: UUID | None = None
    version: int | None = None


class MoveBody(BaseModel):
    parent_id: UUID


class MoveInBody(BaseModel):
    user_ids: list[UUID]


@router.get("/tree")
async def tree(session: SessionDep, p: Principal = require("org:view")) -> list[dict[str, Any]]:
    return await OrgService(session, p.actor).tree()


@router.get("/{dept_id}")
async def detail(
    dept_id: UUID, session: SessionDep, p: Principal = require("org:view")
) -> dict[str, Any]:
    return await OrgService(session, p.actor).detail(dept_id)


@router.post("", status_code=201)
async def create(
    body: DeptCreate, session: SessionDep, p: Principal = require("org:manage")
) -> dict[str, Any]:
    svc = OrgService(session, p.actor)
    d = await svc.create(body.parent_id, body.name)
    return await svc.detail(d.uuid)


@router.patch("/{dept_id}")
async def update(
    dept_id: UUID, body: DeptUpdate, session: SessionDep, p: Principal = require("org:manage")
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    svc = OrgService(session, p.actor)
    await svc.update(
        dept_id,
        name=data.get("name"),
        leader_uuid=data.get("leader_id", UNSET),
        version=data.get("version"),
    )
    return await svc.detail(dept_id)


@router.post("/{dept_id}/move")
async def move(
    dept_id: UUID,
    body: MoveBody,
    session: SessionDep,
    dry_run: bool = False,
    p: Principal = require("org:manage"),
) -> dict[str, Any]:
    return await OrgService(session, p.actor).move(dept_id, body.parent_id, dry_run=dry_run)


@router.delete("/{dept_id}")
async def delete(
    dept_id: UUID, session: SessionDep, dry_run: bool = False, p: Principal = require("org:manage")
) -> dict[str, Any]:
    return await OrgService(session, p.actor).delete(dept_id, dry_run=dry_run)


@router.get("/{dept_id}/members")
async def members(
    dept_id: UUID,
    session: SessionDep,
    include_sub: bool = True,
    q: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    p: Principal = require("org:view"),
) -> dict[str, Any]:
    res = await OrgService(session, p.actor).members(
        dept_id, include_sub=include_sub, q=q, page=Page(page, size)
    )
    creds = (
        {
            c.user_uuid: c
            for c in await session.scalars(
                select(Credential).where(Credential.user_uuid.in_([u.uuid for u in res["items"]]))
            )
        }
        if res["items"]
        else {}
    )
    items = []
    for u in res["items"]:
        items.append(
            user_view(
                u,
                creds.get(u.uuid),
                is_leader=u.uuid == res["leader_uuid"],
                positions=await positions_of(session, u),
            )
        )
    return {"total": res["total"], "items": items}


@router.post("/{dept_id}/members/move-in")
async def move_in(
    dept_id: UUID,
    body: MoveInBody,
    session: SessionDep,
    dry_run: bool = False,
    p: Principal = require("org:manage"),
) -> dict[str, Any]:
    return await OrgService(session, p.actor).move_in(dept_id, body.user_ids, dry_run=dry_run)
