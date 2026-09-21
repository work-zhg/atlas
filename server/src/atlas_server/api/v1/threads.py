from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response, status

from ...deps import CurrentUserDep, RunServiceDep, ThreadServiceDep
from ...schemas.run import RunAccepted, RunCreate
from ...schemas.thread import (
    MessageListOut,
    ThreadCreate,
    ThreadListOut,
    ThreadOut,
    ThreadUpdate,
)

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("", response_model=ThreadListOut)
async def list_threads(
    service: ThreadServiceDep,
    thread_status: Annotated[str | None, Query(alias="status")] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ThreadListOut:
    return await service.list(status=thread_status, cursor=cursor, limit=limit)


@router.post("", response_model=ThreadOut, status_code=status.HTTP_201_CREATED)
async def create_thread(
    payload: ThreadCreate, service: ThreadServiceDep, user_id: CurrentUserDep
) -> ThreadOut:
    return await service.create(payload, user_id=user_id)


@router.get("/{thread_id}", response_model=ThreadOut)
async def get_thread(thread_id: UUID, service: ThreadServiceDep) -> ThreadOut:
    return await service.get(thread_id)


@router.patch("/{thread_id}", response_model=ThreadOut)
async def update_thread(
    thread_id: UUID, payload: ThreadUpdate, service: ThreadServiceDep
) -> ThreadOut:
    """改标题会把 title_source 置为 manual —— 自动生成不再覆盖它（决策 5）。"""
    return await service.update(thread_id, payload)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: UUID, service: ThreadServiceDep) -> Response:
    """真删：message / run / run_file 由 ON DELETE CASCADE 带走。"""
    await service.delete(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{thread_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    thread_id: UUID,
    payload: RunCreate,
    service: RunServiceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RunAccepted:
    """发消息 + 创建 run，立刻返回 run_id；事件走 GET /runs/{id}/events。

    同一会话串行（Redis NX 锁），已有运行中的 run 时返回 409。
    带 Idempotency-Key 时重复提交返回同一个 run，不会变成两条消息（§11.2）。
    """
    return await service.create(thread_id, payload, idempotency_key=idempotency_key)


@router.get("/{thread_id}/messages", response_model=MessageListOut)
async def list_messages(
    thread_id: UUID,
    service: ThreadServiceDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageListOut:
    """倒序分页，返回**完整原文** —— 上下文压缩只改写 checkpoint（§7.4）。"""
    return await service.list_messages(thread_id, cursor=cursor, limit=limit)
