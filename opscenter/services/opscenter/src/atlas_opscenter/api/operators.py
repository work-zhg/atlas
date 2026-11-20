"""运营账号管理（看：任何运营人员；改：运营管理员）。临时密码只出现在响应里。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from ..accounts import Accounts
from ..db.models import Operator
from ..db.types import utcnow
from .deps import AdminDep, OperatorDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api/operators", tags=["operators"])


class OperatorCreate(BaseModel):
    account: str
    name: str
    role: str = "OBSERVER"


class OperatorUpdate(BaseModel):
    name: str | None = None
    role: str | None = None


def operator_view(op: Operator) -> dict[str, Any]:
    now = utcnow()
    return {
        "id": str(op.operator_uuid),
        "account": op.account,
        "name": op.name,
        "role": op.role,
        "status": op.status,
        "locked": op.locked_until is not None and op.locked_until > now,
        "must_change_password": op.must_change_password,
        "last_login_at": op.last_login_at.isoformat() if op.last_login_at else None,
        "created_at": op.created_at.isoformat(),
    }


@router.get("")
async def list_operators(
    _: OperatorDep, session: SessionDep, settings: SettingsDep
) -> list[dict[str, Any]]:
    return [operator_view(o) for o in await Accounts(session, settings).list()]


@router.post("", status_code=201)
async def create_operator(
    body: OperatorCreate, p: AdminDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    op, temp = await Accounts(session, settings).create(body.account, body.name, body.role, p.who)
    return {"operator": operator_view(op), "temp_password": temp}


@router.patch("/{operator_id}")
async def update_operator(
    operator_id: UUID, body: OperatorUpdate, p: AdminDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    return operator_view(await Accounts(session, settings).update(operator_id, data, p.who))


@router.post("/{operator_id}/disable")
async def disable_operator(
    operator_id: UUID, p: AdminDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    return operator_view(await Accounts(session, settings).set_status(operator_id, False, p.who))


@router.post("/{operator_id}/enable")
async def enable_operator(
    operator_id: UUID, p: AdminDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    return operator_view(await Accounts(session, settings).set_status(operator_id, True, p.who))


@router.post("/{operator_id}/reset-password")
async def reset_password(
    operator_id: UUID, p: AdminDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    op, temp = await Accounts(session, settings).reset_password(operator_id, p.who)
    return {"operator": operator_view(op), "temp_password": temp}
