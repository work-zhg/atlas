from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ...auth.authz import PrincipalDep, ensure_run_owner
from ...deps import ApprovalServiceDep, RunServiceDep, SessionDep
from ...errors import NotFound
from ...schemas.run import (
    ApprovalDecision,
    ApprovalOut,
    PendingApproval,
    PendingApprovalList,
    RunOut,
    RunTraceOut,
)

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: UUID, service: RunServiceDep, session: SessionDep, principal: PrincipalDep
) -> RunOut:
    await ensure_run_owner(session, run_id, principal)
    return await service.get(run_id)


@router.get("/{run_id}/trace", response_model=RunTraceOut)
async def get_run_trace(
    run_id: UUID, service: RunServiceDep, session: SessionDep, principal: PrincipalDep
) -> RunTraceOut:
    """一轮的归档轨迹（含子 run）—— 历史轮次的工具、委派、审批靠它在对话里重现。

    ★ 与会话 SSE 流的分工：SSE 只回放最近一个窗口、只服务进行中的那一轮；
      已结束的轮次按需取这里，一轮一次，结果不会再变。
    """
    await ensure_run_owner(session, run_id, principal)
    return await service.trace(run_id)


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: UUID, service: RunServiceDep, session: SessionDep, principal: PrincipalDep
) -> RunOut:
    """写取消信号；engine 每步检查一次，到安全点后产出 run.cancelled。"""
    await ensure_run_owner(session, run_id, principal, write=True)
    return await service.cancel(run_id)


@router.get("/{run_id}/approvals", response_model=PendingApprovalList)
async def list_pending_approvals(
    run_id: UUID, session: SessionDep, principal: PrincipalDep, approvals: ApprovalServiceDep
) -> PendingApprovalList:
    """该 run 上还没决策的确认项。

    存在的理由是**刷新页面**：审批挂起时用户刷新，实时状态全没了。
    SSE 重连虽然会重放 approval.required，但那依赖 Redis 里的事件还在；
    这个端点直接查库，是更硬的恢复路径。
    """
    await ensure_run_owner(session, run_id, principal)
    rows = await approvals.pending(run_id)
    return PendingApprovalList(
        data=[
            PendingApproval(
                id=r.approval_uuid,
                tool_name=r.tool_name,
                args=r.args,
                kind=r.kind,
                reason=r.reason,
                execution_modes=r.execution_modes,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.post("/{run_id}/approvals/{approval_id}", response_model=ApprovalOut)
async def decide_approval(
    run_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecision,
    session: SessionDep,
    principal: PrincipalDep,
    approvals: ApprovalServiceDep,
) -> ApprovalOut:
    """批准或拒绝一次工具调用、规划模式提交的计划（doc/native-approval-mode-design.html），
    或回答智能体提的问题（doc/ask-user-question-design.html）。

    拒绝**不终止 run** —— agent 会收到"用户拒绝"（连同用户给的原因）作为工具结果，
    可以换个方案继续，而不是整轮白跑。批准计划时要选执行模式（手动 / 自动）。
    """
    # ★ 归属按审批**实际所在**的 run 判：只看路径里的 run 会让人拿自己的 run id
    #   去批别人的审批（子智能体的审批可能挂在子 run 上，所以不要求两者相等）
    await ensure_run_owner(session, run_id, principal, write=True)
    # 做了决定由审批组件通知执行器续跑（DecisionListener，接线在 deps.get_approval_service）
    approval = await approvals.get(approval_id)
    if approval is None:
        raise NotFound("确认项不存在", approval_id=str(approval_id))
    await ensure_run_owner(session, approval.run_uuid, principal, write=True)
    await approvals.decide(
        approval_id,
        payload.decision,
        by=principal.uid,
        mode=payload.approval_mode,
        feedback=payload.feedback,
        answers=(
            [a.model_dump() for a in payload.answers] if payload.answers is not None else None
        ),
    )
    return ApprovalOut(approval_id=approval_id, run_id=run_id, decision=payload.decision)
