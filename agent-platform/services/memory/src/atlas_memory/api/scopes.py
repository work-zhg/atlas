"""作用域接口（§3.2）。

GET   /v1/scopes                 列出作用域（不需要身份：配置不敏感，客户端据此知道字段）
GET   /v1/scopes/{scope}
POST  /v1/admin/scopes           新建（要 memory:scope:manage）
PATCH /v1/admin/scopes/{scope}   改描述、数据权限开关、数据编码（字段不能改）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..auth import Operator, require_operator
from ..scopes import ScopeService, scope_view
from .deps import SessionDep
from .schemas import ScopeCreate, ScopePatch

router = APIRouter(tags=["scopes"])

OperatorDep = Annotated[Operator, Depends(require_operator)]


@router.get("/v1/scopes")
async def list_scopes(session: SessionDep) -> dict[str, Any]:
    return {"data": [scope_view(s) for s in await ScopeService(session).list()]}


@router.get("/v1/scopes/{scope}")
async def get_scope(scope: str, session: SessionDep) -> dict[str, Any]:
    return scope_view(await ScopeService(session).get(scope))


@router.post("/v1/admin/scopes", status_code=201)
async def create_scope(body: ScopeCreate, session: SessionDep, op: OperatorDep) -> dict[str, Any]:
    row = await ScopeService(session).create(
        code=body.code,
        description=body.description,
        key_fields=body.key_fields,
        check_permission=body.check_permission,
        data_code=body.data_code,
        actor=op.uid,
    )
    return scope_view(row)


@router.patch("/v1/admin/scopes/{scope}")
async def update_scope(
    scope: str, body: ScopePatch, session: SessionDep, _: OperatorDep
) -> dict[str, Any]:
    row = await ScopeService(session).update(
        scope,
        description=body.description,
        check_permission=body.check_permission,
        data_code=body.data_code,
    )
    return scope_view(row)
