from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ...deps import CurrentUserDep, ExecutorDep, RunServiceDep, SessionDep
from ...errors import Conflict
from ...repositories.approval import ApprovalRepository
from ...schemas.run import (
    ApprovalDecision,
    ApprovalOut,
    PendingApproval,
    PendingApprovalList,
    RunOut,
    RunTraceOut,
)
from ...services.approval import submit_decision

router = APIRouter(prefix="/runs", tags=["runs"])

@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, service: RunServiceDep) -> RunOut:
    return await service.get(run_id)


@router.get("/{run_id}/trace", response_model=RunTraceOut)
async def get_run_trace(run_id: UUID, service: RunServiceDep) -> RunTraceOut:
    """一轮的归档轨迹（含子 run）—— 历史轮次的工具、委派、审批靠它在对话里重现。

    ★ 与会话 SSE 流的分工：SSE 只回放最近一个窗口、只服务进行中的那一轮；
      已结束的轮次按需取这里，一轮一次，结果不会再变。
    """
    return await service.trace(run_id)


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
    user_id: CurrentUserDep,
    executor: ExecutorDep,
) -> ApprovalOut:
    """批准或拒绝一次高风险工具调用（§12.2）。

    拒绝**不终止 run** —— agent 会收到"用户拒绝"作为工具结果，
    可以换个方案继续，而不是整轮白跑。
    """
    ok = await submit_decision(
        session, approval_id=approval_id, decision=payload.decision, user_id=user_id
    )
    if not ok:
        raise Conflict("该确认已被处理过或已超时", approval_id=str(approval_id))

    # ★ 决策到了 —— 立刻唤醒那个挂起的 run。
    #
    #   审批现在是挂起而不是在线阻塞（detail/suspension.html §06）：run 落成
    #   awaiting_approval 之后**没有进程**在等这个决策。不在这里唤醒的话要等到
    #   周期扫描（默认 60s），用户点完「允许」得干等一分钟，看着像没生效。
    #
    #   ★ 幂等：resume_if_ready 内部有 CAS，与周期扫描撞上时只有一个能赢。
    await executor.resume_if_ready(run_id)
    return ApprovalOut(approval_id=approval_id, run_id=run_id, decision=payload.decision)
