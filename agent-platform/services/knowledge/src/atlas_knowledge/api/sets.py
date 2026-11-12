"""/v1/knowledge-sets 知识集（doc/design/knowledge-set-design.html）。

集里的库用 GET /v1/datasets?set_id= 列；建库、移库走 /v1/datasets（带 set_id）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services.sets import MAX_MAINTAINERS, KnowledgeSetService
from .deps import PrincipalDep, RuntimeDep, SessionDep

router = APIRouter(prefix="/v1/knowledge-sets", tags=["knowledge-sets"])


class SetBody(BaseModel):
    name: str | None = None
    #: 给智能体看的介绍：里面有什么、能回答哪类问题
    description: str | None = None
    icon: str | None = None
    #: 智能体在这个知识集里一次取几条
    top_k: int | None = None
    #: 用户 id；不给 = 只有建的人
    maintainers: list[str] | None = Field(default=None, max_length=MAX_MAINTAINERS)


@router.get("")
async def list_sets(
    session: SessionDep, rt: RuntimeDep, p: PrincipalDep, keyword: str | None = None
) -> dict[str, Any]:
    return {"data": await KnowledgeSetService(session, rt).list(p, keyword=keyword)}


@router.post("", status_code=201)
async def create_set(
    body: SetBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    svc = KnowledgeSetService(session, rt)
    ks = await svc.create(p, body.model_dump(exclude_unset=True))
    return await svc.get(ks.uuid, p)


@router.get("/{set_id}")
async def get_set(
    set_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    return await KnowledgeSetService(session, rt).get(set_id, p)


@router.patch("/{set_id}")
async def update_set(
    set_id: UUID, body: SetBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    svc = KnowledgeSetService(session, rt)
    await svc.update(set_id, p, body.model_dump(exclude_unset=True))
    return await svc.get(set_id, p)


@router.delete("/{set_id}", status_code=204)
async def delete_set(set_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep) -> None:
    await KnowledgeSetService(session, rt).delete(set_id, p)
