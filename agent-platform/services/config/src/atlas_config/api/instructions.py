"""持久指令（doc/context-management-design.html §10）。

    GET/PUT /config/instructions/me         当前用户的个人指令（任何登录用户）
    GET     /config/instructions/platform   平台指令（任何登录用户可读）
    PUT     /config/instructions/platform   平台指令（instruction:manage 或 model:manage）
    GET     /internal/instructions?user_id= 运行时读取：{platform, user}

★ 每层有长度上限：指令每轮都进上下文，塞太多既贵又稀释注意力。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..audit import record
from ..auth import Principal, current_principal, require_any, require_internal
from ..db.models import Instruction
from ..db.types import utcnow
from .deps import SessionDep

__all__ = ["internal_router", "router"]

#: 每层的字符上限（约 4k tokens）
MAX_CHARS = 10_000

router = APIRouter(prefix="/config/instructions", tags=["instructions"])
internal_router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal)]
)

User = Annotated[Principal, Depends(current_principal)]
PlatformAdmin = Annotated[Principal, Depends(require_any("instruction:manage", "model:manage"))]


class InstructionIn(BaseModel):
    content: str = Field(default="", max_length=MAX_CHARS)


class InstructionOut(BaseModel):
    scope: str
    content: str
    updated_at: datetime | None = None
    updated_by: str | None = None
    max_chars: int = MAX_CHARS


async def _get(session: Any, scope: str, owner: str) -> Instruction | None:
    return await session.scalar(
        select(Instruction).where(Instruction.scope == scope, Instruction.owner_id == owner)
    )


def _out(scope: str, row: Instruction | None) -> InstructionOut:
    if row is None:
        return InstructionOut(scope=scope, content="")
    return InstructionOut(
        scope=scope, content=row.content, updated_at=row.updated_at, updated_by=row.updated_by
    )


async def _put(session: Any, scope: str, owner: str, content: str, actor: str) -> InstructionOut:
    row = await _get(session, scope, owner)
    if row is None:
        row = Instruction(scope=scope, owner_id=owner, content=content, updated_by=actor)
        session.add(row)
    else:
        row.content = content
        row.updated_by = actor
        row.updated_at = utcnow()
    record(session, actor=actor, action="instruction.update", target_kind="instruction",
           target_id=f"{scope}:{owner}", chars=len(content))  # fmt: skip
    await session.commit()
    return _out(scope, row)


@router.get("/me", response_model=InstructionOut)
async def get_mine(session: SessionDep, p: User) -> InstructionOut:
    return _out("user", await _get(session, "user", p.user))


@router.put("/me", response_model=InstructionOut)
async def put_mine(body: InstructionIn, session: SessionDep, p: User) -> InstructionOut:
    return await _put(session, "user", p.user, body.content.strip(), p.user)


@router.get("/platform", response_model=InstructionOut)
async def get_platform(session: SessionDep, p: User) -> InstructionOut:
    return _out("platform", await _get(session, "platform", ""))


@router.put("/platform", response_model=InstructionOut)
async def put_platform(
    body: InstructionIn, session: SessionDep, p: PlatformAdmin
) -> InstructionOut:
    return await _put(session, "platform", "", body.content.strip(), p.user)


@internal_router.get("/instructions")
async def runtime_instructions(
    session: SessionDep, user_id: Annotated[str, Query(min_length=1)]
) -> dict[str, str]:
    platform = await _get(session, "platform", "")
    user = await _get(session, "user", user_id)
    return {"platform": platform.content if platform else "", "user": user.content if user else ""}
