"""服务账号（权限设计 §10）：管理员创建、停用、轮换密钥。密钥只在创建 / 轮换时返回一次。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...auth.authz import require
from ...auth.catalog import ALL_OPERATIONS
from ...auth.principal import Principal
from ...auth.service import hash_secret, new_service_key
from ...db.models import ServiceAccount
from ...deps import SessionDep
from ...errors import AppError, Conflict, NotFound

router = APIRouter(prefix="/service-accounts", tags=["service-accounts"])

Admin = Annotated[Principal, Depends(require("service_account:manage"))]


class InvalidOperations(AppError):
    kind = "invalid_operations"


class ServiceAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    operations: list[str] = ["chat:use"]
    agent_scope: Literal["all_enabled", "list"] = "all_enabled"
    agent_ids: list[UUID] = []
    allow_acting: bool = False
    memory_default: Literal["on", "off"] = "off"


class ServiceAccountPatch(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    operations: list[str] | None = None
    agent_scope: Literal["all_enabled", "list"] | None = None
    agent_ids: list[UUID] | None = None
    allow_acting: bool | None = None
    memory_default: Literal["on", "off"] | None = None
    status: Literal["active", "disabled"] | None = None


class ServiceAccountOut(BaseModel):
    id: UUID
    name: str
    description: str
    key_prefix: str
    operations: list[str]
    agent_scope: str
    agent_ids: list[UUID]
    allow_acting: bool
    memory_default: str
    status: str
    created_at: datetime
    last_used_at: datetime | None
    #: 只在创建 / 轮换时有值
    key: str | None = None


def _out(sa: ServiceAccount, key: str | None = None) -> ServiceAccountOut:
    return ServiceAccountOut(
        id=sa.id,
        name=sa.name,
        description=sa.description,
        key_prefix=sa.key_prefix,
        operations=list(sa.operations or []),
        agent_scope=sa.agent_scope,
        agent_ids=[UUID(str(a)) for a in sa.agent_ids or []],
        allow_acting=sa.allow_acting,
        memory_default=sa.memory_default,
        status=sa.status,
        created_at=sa.created_at,
        last_used_at=sa.last_used_at,
        key=key,
    )


def _check_ops(ops: list[str]) -> list[str]:
    bad = sorted(set(ops) - ALL_OPERATIONS)
    if bad:
        raise InvalidOperations("未知的操作码", operations=",".join(bad))
    return sorted(set(ops))


async def _get(session: SessionDep, sid: UUID) -> ServiceAccount:
    sa = await session.get(ServiceAccount, sid)
    if sa is None:
        raise NotFound("服务账号不存在", id=str(sid))
    return sa


@router.get("", response_model=list[ServiceAccountOut])
async def list_service_accounts(session: SessionDep, _: Admin) -> list[ServiceAccountOut]:
    rows = await session.scalars(select(ServiceAccount).order_by(ServiceAccount.created_at))
    return [_out(r) for r in rows]


@router.post("", response_model=ServiceAccountOut, status_code=status.HTTP_201_CREATED)
async def create_service_account(
    body: ServiceAccountIn, session: SessionDep, admin: Admin
) -> ServiceAccountOut:
    if await session.scalar(select(ServiceAccount.id).where(ServiceAccount.name == body.name)):
        raise Conflict("服务账号名称已存在", name=body.name)
    key, prefix = new_service_key()
    sa = ServiceAccount(
        name=body.name,
        description=body.description,
        key_prefix=prefix,
        key_hash=hash_secret(key),
        operations=_check_ops(body.operations),
        agent_scope=body.agent_scope,
        agent_ids=[str(a) for a in body.agent_ids],
        allow_acting=body.allow_acting,
        memory_default=body.memory_default,
        created_by=admin.owner_id,
    )
    session.add(sa)
    await session.flush()
    await session.refresh(sa)
    return _out(sa, key)


@router.patch("/{sid}", response_model=ServiceAccountOut)
async def update_service_account(
    sid: UUID, body: ServiceAccountPatch, session: SessionDep, _: Admin
) -> ServiceAccountOut:
    sa = await _get(session, sid)
    data: dict[str, Any] = body.model_dump(exclude_unset=True)
    if "operations" in data and data["operations"] is not None:
        data["operations"] = _check_ops(data["operations"])
    if "agent_ids" in data and data["agent_ids"] is not None:
        data["agent_ids"] = [str(a) for a in data["agent_ids"]]
    for k, v in data.items():
        if v is not None:
            setattr(sa, k, v)
    await session.flush()
    return _out(sa)


@router.post("/{sid}/rotate-key", response_model=ServiceAccountOut)
async def rotate_service_account_key(sid: UUID, session: SessionDep, _: Admin) -> ServiceAccountOut:
    """轮换后旧密钥立即失效。"""
    sa = await _get(session, sid)
    key, prefix = new_service_key()
    sa.key_hash, sa.key_prefix = hash_secret(key), prefix
    await session.flush()
    return _out(sa, key)
