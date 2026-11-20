"""作用域接口（§3.2）。

GET   /v1/scopes                 列出作用域（不需要身份：配置不敏感，客户端据此知道字段）
GET   /v1/scopes/{scope}
GET   /ops/v1/scopes             运营中心看作用域（运营接口：先验签名）
POST  /ops/v1/scopes             新建（运营接口：只给运营中心）
PATCH /ops/v1/scopes/{scope}     改描述、数据权限开关、数据编码（字段不能改）

★ 作用域是产品自带的定义（多租户设计 §2）：所有租户共用，随应用版本登记（部署时用命令行
  scope add --if-missing），租户里谁都改不了；原来按 memory_scope:manage 判的管理接口移到运营接口。
"""

from __future__ import annotations

from typing import Annotated, Any

from atlas_opsapi import OperatorStamp
from fastapi import APIRouter, Depends, Request

from ..scopes import ScopeService, scope_view
from .deps import SessionDep
from .schemas import ScopeCreate, ScopePatch

router = APIRouter(tags=["scopes"])


async def _operator(request: Request) -> OperatorStamp:
    return await request.app.state.ops_guard.verify(request)  # type: ignore[no-any-return]


OperatorDep = Annotated[OperatorStamp, Depends(_operator)]


@router.get("/v1/scopes")
async def list_scopes(session: SessionDep) -> dict[str, Any]:
    return {"data": [scope_view(s) for s in await ScopeService(session).list()]}


@router.get("/v1/scopes/{scope}")
async def get_scope(scope: str, session: SessionDep) -> dict[str, Any]:
    return scope_view(await ScopeService(session).get(scope))


@router.get("/ops/v1/scopes")
async def ops_list_scopes(session: SessionDep, _: OperatorDep) -> dict[str, Any]:
    return {"data": [scope_view(s) for s in await ScopeService(session).list()]}


@router.post("/ops/v1/scopes", status_code=201)
async def create_scope(body: ScopeCreate, session: SessionDep, op: OperatorDep) -> dict[str, Any]:
    row = await ScopeService(session).create(
        code=body.code,
        description=body.description,
        key_fields=body.key_fields,
        check_permission=body.check_permission,
        data_code=body.data_code,
        actor=op.who[:64],
    )
    return scope_view(row)


@router.patch("/ops/v1/scopes/{scope}")
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
