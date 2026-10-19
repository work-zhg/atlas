"""/internal/v1：atlas-server 调用，不对浏览器开放（网关返回 404）。

内网免令牌（内网身份传递 §3）：调用方带身份请求头，X-Uid 谁在用、X-Auth-Uid 按谁判。

POST /internal/v1/retrieve            智能体运行时检索（库级权限已在运行时按智能体绑定判过；
                                      文档级权限按 X-Auth-Uid）
POST /internal/v1/datasets/readable   绑定知识库时校验：auth-uid 能读哪些库（返回名称与描述）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db.models import Dataset
from ..services.permission import visible_clause
from ..services.retrieval import KnowledgeRetrieval, RetrieveOptions
from .deps import PrincipalDep, RuntimeDep, SessionDep

router = APIRouter(prefix="/internal/v1", include_in_schema=False)


class InternalRetrieveBody(BaseModel):
    dataset_ids: list[UUID] = Field(min_length=1, max_length=20)
    query: str
    retrieval_config: dict[str, Any] | None = None
    #: agent | mcp
    source: str = "agent"
    #: 线程 / run 引用，进检索记录
    source_ref: str | None = None
    #: 兼容旧调用方：没有身份请求头时当作发起人（新调用方用 X-Uid / X-Auth-Uid）
    actor: str | None = None
    #: 对话历史 [{role, content}]（问题含指代时改写，§7.1）
    history: list[dict[str, str]] | None = Field(default=None, max_length=50)
    filter: dict[str, Any] | None = None
    as_of: datetime | None = None
    include_superseded: bool = False


@router.post("/retrieve")
async def internal_retrieve(
    body: InternalRetrieveBody, request: Request, session: SessionDep, rt: RuntimeDep
) -> dict[str, Any]:
    uid = request.headers.get("x-uid") or body.actor
    res = await KnowledgeRetrieval(session, rt).retrieve(
        None,
        body.dataset_ids,
        body.query,
        body.retrieval_config,
        source=body.source if body.source in ("agent", "mcp") else "agent",
        source_ref=body.source_ref,
        actor=uid,
        acl_subject=request.headers.get("x-auth-uid") or uid,
        opts=RetrieveOptions(
            history=body.history,
            filter=body.filter,
            as_of=body.as_of,
            include_superseded=body.include_superseded,
        ),
    )
    out = res.as_dict(body.query)
    out.pop("trace_id", None)
    out.pop("elapsed_ms", None)
    return out


class ReadableBody(BaseModel):
    dataset_ids: list[UUID] | None = None


@router.post("/datasets/readable")
async def readable(
    body: ReadableBody, p: PrincipalDep, session: SessionDep, rt: RuntimeDep
) -> dict[str, Any]:
    """智能体编辑时「能挂哪些库」：auth-uid 的用户中心数据权限（≥ READ）。"""
    q = select(Dataset).where(await visible_clause(rt.perms, p))
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
