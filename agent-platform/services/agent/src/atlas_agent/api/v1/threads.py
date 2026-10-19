from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool

from ...auth.authz import PrincipalDep, ensure_thread_owner
from ...deps import (
    ExecutorDep,
    RedisDep,
    RunServiceDep,
    SessionDep,
    SettingsDep,
    ThreadServiceDep,
)
from ...repositories.thread import ThreadRepository
from ...schemas.run import RunAccepted, RunCreate
from ...schemas.thread import (
    MessageListOut,
    PreviewSessionOut,
    ThreadCreate,
    ThreadListOut,
    ThreadOut,
    ThreadUpdate,
    WorkspaceFilesOut,
)
from ...services.context import ContextService
from ...services.preview import PreviewService
from ...services.thread import BadRequest

router = APIRouter(prefix="/threads", tags=["threads"])

# nginx 会缓冲响应导致"流式"变成一次性吐出；关掉它与 gzip（§10.3）
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("", response_model=ThreadListOut)
async def list_threads(
    service: ThreadServiceDep,
    principal: PrincipalDep,
    thread_status: Annotated[str | None, Query(alias="status")] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ThreadListOut:
    """只列本主体的根会话（权限设计 §7.4）。"""
    return await service.list(owner=principal.uid, status=thread_status, cursor=cursor, limit=limit)


@router.post("", response_model=ThreadOut, status_code=status.HTTP_201_CREATED)
async def create_thread(
    payload: ThreadCreate, service: ThreadServiceDep, principal: PrincipalDep
) -> ThreadOut:
    return await service.create(payload, principal=principal)


@router.get("/{thread_id}", response_model=ThreadOut)
async def get_thread(
    thread_id: UUID, service: ThreadServiceDep, session: SessionDep, principal: PrincipalDep
) -> ThreadOut:
    await ensure_thread_owner(session, thread_id, principal)
    return await service.get(thread_id)


@router.patch("/{thread_id}", response_model=ThreadOut)
async def update_thread(
    thread_id: UUID,
    payload: ThreadUpdate,
    service: ThreadServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> ThreadOut:
    """改标题会把 title_source 置为 manual —— 自动生成不再覆盖它（决策 5）。"""
    await ensure_thread_owner(session, thread_id, principal, write=True)
    return await service.update(thread_id, payload)


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: UUID, service: ThreadServiceDep, session: SessionDep, principal: PrincipalDep
) -> Response:
    """真删：message / run / run_file 由 ON DELETE CASCADE 带走。"""
    await ensure_thread_owner(session, thread_id, principal, write=True)
    await service.delete(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{thread_id}/files", response_model=WorkspaceFilesOut)
async def list_files(
    thread_id: UUID, service: ThreadServiceDep, session: SessionDep, principal: PrincipalDep
) -> WorkspaceFilesOut:
    """会话工作区里的文件 —— 直接列对象存储，包括 acp 子智能体在 Pod 里写的产物。"""
    await ensure_thread_owner(session, thread_id, principal)
    return await service.list_files(thread_id)


@router.post("/{thread_id}/files/preview", response_model=PreviewSessionOut)
async def create_preview(
    thread_id: UUID,
    request: Request,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    principal: PrincipalDep,
) -> PreviewSessionOut:
    """签发文件预览令牌。站点在 GET /v1/previews/{token}/… 下（见 api/v1/previews.py）。

    ★ 令牌本身就是访问凭据（预览站点不带 Cookie）：只给会话所有者签发。
    """
    await ensure_thread_owner(session, thread_id, principal)
    service = PreviewService(redis, settings)
    token, expires_at = await service.issue(ThreadRepository(session), thread_id)
    return PreviewSessionOut(
        base_url=service.base_url(token, request_base=str(request.base_url)),
        expires_at=expires_at,
    )


@router.get("/{thread_id}/files/download")
async def download_file(
    thread_id: UUID,
    service: ThreadServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
    path: Annotated[str, Query(min_length=1)],
) -> StreamingResponse:
    """下载工作区里的一个文件（流式，不整个读进内存）。"""
    await ensure_thread_owner(session, thread_id, principal)
    body, size = await service.open_file(thread_id, path)
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    headers = {
        # RFC 5987：中文文件名要走 filename*，否则浏览器拿到的是乱码
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "Content-Length": str(size),
    }
    return StreamingResponse(
        iterate_in_threadpool(body.iter_chunks(64 * 1024)),
        media_type="application/octet-stream",
        headers=headers,
    )


#: 单个上传文件的上限。大文件该走对象存储直传，而不是经 server 中转
UPLOAD_MAX_BYTES = 10 * 1024 * 1024


@router.put("/{thread_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    thread_id: UUID,
    request: Request,
    service: ThreadServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict[str, Any]:
    """把请求体原样写成工作区里的一个文件（覆盖同名）。与 download 对称。

    用途：用户给智能体准备输入文件；评测给任务准备初始工作区（doc/agent-eval-design.html）。
    """
    await ensure_thread_owner(session, thread_id, principal, write=True)
    data = await request.body()
    if len(data) > UPLOAD_MAX_BYTES:
        raise BadRequest(f"文件超过 {UPLOAD_MAX_BYTES // 1024 // 1024} MB", path=path)
    size = await service.put_file(thread_id, path, data)
    return {"path": path, "size": size}


class CompactIn(BaseModel):
    #: 可选：希望摘要重点保留什么
    focus: str = Field(default="", max_length=2000)


@router.get("/{thread_id}/context")
async def get_context(
    thread_id: UUID, session: SessionDep, settings: SettingsDep, principal: PrincipalDep
) -> dict[str, Any]:
    """会话的上下文状态：最近一次模型调用的构成与用量、摘要、熔断（上下文管理 §16）。"""
    await ensure_thread_owner(session, thread_id, principal)
    return await ContextService(session, settings, None).status(thread_id)


@router.post("/{thread_id}/compact")
async def compact_thread(
    thread_id: UUID,
    body: CompactIn,
    session: SessionDep,
    settings: SettingsDep,
    executor: ExecutorDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    """手动压缩：把摘要之后的全部历史并入新摘要，下一轮生效（上下文管理 §16）。"""
    await ensure_thread_owner(session, thread_id, principal, write=True)
    builder = getattr(executor, "model_builder", None)
    return await ContextService(session, settings, builder).compact(thread_id, focus=body.focus)


@router.post(
    "/{thread_id}/runs",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    thread_id: UUID,
    payload: RunCreate,
    service: RunServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RunAccepted:
    """发消息 + 创建 run，立刻返回 run_id；事件走 GET /runs/{id}/events。

    同一会话串行（Redis NX 锁），已有运行中的 run 时返回 409。
    带 Idempotency-Key 时重复提交返回同一个 run，不会变成两条消息（§11.2）。
    """
    await ensure_thread_owner(session, thread_id, principal, write=True)
    # 幂等键按使用者隔离：否则别人用同一个键能拿到这个 run
    key = f"{principal.uid}:{idempotency_key}" if idempotency_key else None
    return await service.create(
        thread_id,
        payload,
        idempotency_key=key,
        initiator=principal.uid,
        auth_uid=principal.auth_uid,
    )


@router.get("/{thread_id}/events")
async def stream_thread_events(
    thread_id: UUID,
    request: Request,
    service: RunServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """一条会话的 SSE 事件流 —— 该会话下**所有** run 的事件，含子智能体的。

    与 `GET /runs/{id}/events` 的三处差别（doc/detail/suspension.html §04）：

      · 永不自动结束。终态事件不再是关闭信号 —— 一轮跑完后面还有下一轮。
        这是子 run 的过程（含审批弹窗）能被看见的前提。
      · id 写 `thread_seq`（会话内单调），不是 run 内的 seq。
      · 首连只回放最近 N 条（sse_thread_replay_events）。带游标的重连不受
        此限 —— 那时要补的是缺口。

    ★ 游标缺省值是 None 而不是 0，两者语义不同：None = 「我没有游标，给我
      默认窗口」，0 = 「从头给我」。用 0 当缺省会让每次打开老会话都全量回放。
    """
    resume_from: int | None = None
    if last_event_id and last_event_id.isdigit():
        resume_from = int(last_event_id)
    if after_seq is not None:
        resume_from = after_seq if resume_from is None else max(resume_from, after_seq)

    # 先探一次（含归属），让 404 以 JSON 形式返回，而不是变成一个空的 200 流
    await ensure_thread_owner(session, thread_id, principal)
    await service.ensure_thread(thread_id)

    gate = request.app.state.stream_gate
    return StreamingResponse(
        gate.guard(service.stream_thread(thread_id, after_seq=resume_from)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/{thread_id}/messages", response_model=MessageListOut)
async def list_messages(
    thread_id: UUID,
    service: ThreadServiceDep,
    session: SessionDep,
    principal: PrincipalDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageListOut:
    """倒序分页，返回**完整原文** —— 上下文压缩只改写 checkpoint（§7.4）。"""
    await ensure_thread_owner(session, thread_id, principal)
    return await service.list_messages(thread_id, cursor=cursor, limit=limit)
