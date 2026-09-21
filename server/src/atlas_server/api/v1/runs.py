from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from ...deps import CurrentUserDep, RedisDep, RunServiceDep, SessionDep
from ...errors import Conflict
from ...repositories.approval import ApprovalRepository
from ...schemas.run import (
    ApprovalDecision,
    ApprovalOut,
    PendingApproval,
    PendingApprovalList,
    RunOut,
)
from ...services.approval import submit_decision

router = APIRouter(prefix="/runs", tags=["runs"])

# nginx 会缓冲响应导致"流式"变成一次性吐出；关掉它与 gzip（§10.3）
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, service: RunServiceDep) -> RunOut:
    return await service.get(run_id)


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: UUID, service: RunServiceDep) -> RunOut:
    """写取消信号；engine 每步检查一次，到安全点后产出 run.cancelled。"""
    return await service.cancel(run_id)


@router.get("/{run_id}/approvals", response_model=PendingApprovalList)
async def list_pending_approvals(run_id: UUID, session: SessionDep) -> PendingApprovalList:
    """该 run 上还没决策的确认项。

    存在的理由是**刷新页面**：审批挂起时用户刷新，实时状态全没了。
    SSE 重连虽然会重放 approval.required，但那依赖 Redis 里的事件还在；
    这个端点直接查库，是更硬的恢复路径。
    """
    rows = await ApprovalRepository(session).list_pending(run_id)
    return PendingApprovalList(
        data=[
            PendingApproval(id=r.id, tool_name=r.tool_name, args=r.args, created_at=r.created_at)
            for r in rows
        ]
    )


@router.post("/{run_id}/approvals/{approval_id}", response_model=ApprovalOut)
async def decide_approval(
    run_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    session: SessionDep,
    redis: RedisDep,
    user_id: CurrentUserDep,
) -> ApprovalOut:
    """批准或拒绝一次高风险工具调用（§12.2）。

    拒绝**不终止 run** —— agent 会收到"用户拒绝"作为工具结果，
    可以换个方案继续，而不是整轮白跑。
    """
    ok = await submit_decision(
        session, redis, approval_id=approval_id, decision=payload.decision, user_id=user_id
    )
    if not ok:
        raise Conflict("该确认已被处理过或已超时", approval_id=str(approval_id))
    return ApprovalOut(approval_id=approval_id, run_id=run_id, decision=payload.decision)


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: UUID,
    service: RunServiceDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """SSE 事件流。

    断线重连：浏览器 EventSource 自动带 Last-Event-ID（即上次收到的 seq），
    服务端据此补发缺失部分再续读 —— 前端不需要写重连逻辑（§10.2）。
    after_seq 查询参数供非浏览器客户端使用。
    """
    resume_from = 0
    if last_event_id and last_event_id.isdigit():
        resume_from = int(last_event_id)
    if after_seq is not None:
        resume_from = max(resume_from, after_seq)

    # 先探一次，让 404 以 JSON 形式返回，而不是变成一个空的 200 流
    await service.get(run_id)

    return StreamingResponse(
        service.stream(run_id, after_seq=resume_from),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
