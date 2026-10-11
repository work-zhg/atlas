"""/internal/v1：atlas-server 调用（X-Internal-Token），不对浏览器开放（网关返回 404）。

POST /internal/v1/retrieve            智能体运行时检索（权限已在运行时按智能体绑定判过）
POST /internal/v1/datasets/readable   绑定知识库时校验：某个主体能读哪些库（返回名称与描述）
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import Principal, require_internal
from ..db.models import Dataset
from ..services.permission import visible_clause
from ..services.retrieval import KnowledgeRetrieval
from .deps import RuntimeDep, SessionDep

router = APIRouter(
    prefix="/internal/v1", include_in_schema=False, dependencies=[Depends(require_internal)]
)


class InternalRetrieveBody(BaseModel):
    dataset_ids: list[UUID] = Field(min_length=1, max_length=20)
    query: str
    retrieval_config: dict[str, Any] | None = None
    #: agent | mcp
    source: str = "agent"
    #: 线程 / run 引用，进检索记录
    source_ref: str | None = None
    #: 这一轮的发起人（进检索记录）
    actor: str | None = None


@router.post("/retrieve")
async def internal_retrieve(
    body: InternalRetrieveBody, session: SessionDep, rt: RuntimeDep
) -> dict[str, Any]:
    records = await KnowledgeRetrieval(session, rt).retrieve(
        None,
        body.dataset_ids,
        body.query,
        body.retrieval_config,
        source=body.source if body.source in ("agent", "mcp") else "agent",
        source_ref=body.source_ref,
        actor=body.actor,
    )
    return {"query": body.query, "records": [r.as_dict() for r in records]}


class ReadableBody(BaseModel):
    principal_id: str
    operations: list[str] = []
    dataset_ids: list[UUID] | None = None


@router.post("/datasets/readable")
async def readable(body: ReadableBody, session: SessionDep) -> dict[str, Any]:
    p = Principal(id=body.principal_id, operations=frozenset(body.operations))
    q = select(Dataset).where(visible_clause(p))
    if body.dataset_ids is not None:
        q = q.where(Dataset.uuid.in_(body.dataset_ids))
    rows = (await session.scalars(q.order_by(Dataset.name))).all()
    return {
        "data": [
            {
                "id": str(d.uuid),
                "name": d.name,
                "description": d.description,
                "icon": d.icon,
                "embedding_provider": d.embedding_provider,
                "embedding_model": d.embedding_model,
            }
            for d in rows
        ]
    }
