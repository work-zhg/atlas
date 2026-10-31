"""持久指令（doc/context-management-design.html §10）。

    GET|PUT /instructions/me         当前用户的个人指令
    GET     /instructions/platform   平台指令（登录即可读）
    PUT     /instructions/platform   平台指令                                 model:manage

★ 修改从下一轮生效：运行时每轮开始时读一次（executor/context.py），本轮内不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...auth.authz import require
from ...auth.principal import Principal
from ...db.models import Instruction
from ...deps import CurrentUserDep, SessionDep
from ...services.instruction import MAX_CHARS, PLATFORM, USER, InstructionService, owner_of

router = APIRouter(prefix="/instructions", tags=["instructions"])

PlatformAdmin = Annotated[Principal, Depends(require("model:manage"))]


class InstructionIn(BaseModel):
    content: str = Field(default="", max_length=MAX_CHARS)


class InstructionOut(BaseModel):
    scope: str
    content: str
    updated_at: datetime | None = None
    updated_by: UUID | None = None
    max_chars: int = MAX_CHARS


def _out(scope: str, row: Instruction | None) -> InstructionOut:
    if row is None:
        return InstructionOut(scope=scope, content="")
    return InstructionOut(
        scope=scope, content=row.content, updated_at=row.updated_at, updated_by=row.updated_by
    )


async def _put(session: SessionDep, scope: str, uid: UUID, content: str) -> InstructionOut:
    row = await InstructionService(session).put(
        scope, owner_of(scope, uid), content.strip(), actor=uid
    )
    await session.commit()
    return _out(scope, row)


@router.get("/me", response_model=InstructionOut)
async def get_mine(session: SessionDep, uid: CurrentUserDep) -> InstructionOut:
    return _out(USER, await InstructionService(session).get(USER, owner_of(USER, uid)))


@router.put("/me", response_model=InstructionOut)
async def put_mine(body: InstructionIn, session: SessionDep, uid: CurrentUserDep) -> InstructionOut:
    return await _put(session, USER, uid, body.content)


@router.get("/platform", response_model=InstructionOut)
async def get_platform(session: SessionDep) -> InstructionOut:
    return _out(PLATFORM, await InstructionService(session).get(PLATFORM, ""))


@router.put("/platform", response_model=InstructionOut)
async def put_platform(
    body: InstructionIn, session: SessionDep, principal: PlatformAdmin
) -> InstructionOut:
    return await _put(session, PLATFORM, principal.uid, body.content)
