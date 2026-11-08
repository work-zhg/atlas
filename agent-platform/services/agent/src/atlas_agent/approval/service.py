"""审批：等人决定的事（c4-model/agent/code-approval.html）。

工具调用的确认、规划模式提交的计划、向用户的提问 —— 三者一张表（approval），原生智能体和
CLI 智能体共用、弹同一个窗口：

    登记、查状态    RunApprovalGate.check（原生运行时的审批、提问中间件；CLI 的权限转发经它）
                    第一次查时登记，立刻返回，不阻塞 —— 等待交给数据库，可以等到第二天
    做决定          ApprovalService.decide：校验执行模式和答案，条件更新只成功一次，
                    然后通知 DecisionListener（Run 执行器）续跑、发决定信号（DecisionChannel）
    在线等待        OnlineApprovals.wait：CLI 的权限请求在线等决定 —— 等决定信号，回库兜底
    续跑前核对      ApprovalService.status / result（Run 执行器）
    作废            ApprovalService.expire_run：run 进入终态时，还挂着的一律 expired

它不判断该不该问（原生运行时按审批模式和工具类别、CLI 按自己的权限模式），不发
approval.required 事件（运行时发），也不续跑（Run 执行器）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from atlas_engine.contracts import ApprovalState, Decision
from atlas_engine.runtime import resolve_answers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Approval
from ..errors import Conflict, InvalidAnswers, InvalidApprovalMode, NotFound
from ..repositories.approval import ApprovalRepository
from ..telemetry import semconv as _sc
from ..telemetry import tracer as _tracer
from .channel import DecisionChannel

logger = logging.getLogger(__name__)


class DecisionListener(Protocol):
    """做了决定通知谁。由 Run 执行器实现：等的事项都有结果了就放回队列续跑。

    ★ 审批因此不认识执行器：执行器既要查事项状态（依赖审批），又要被审批唤醒，唤醒做成
      这里定义的接口，依赖就是单向的。唤醒丢了也不要紧：执行器有定时补扫。
    """

    async def decided(self, run_id: UUID, approval_id: UUID) -> None: ...


class ApprovalService:
    """等人决定的事的入口（按请求或段的会话构造）：做决定、待决事项、续跑前核对、作废。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        listener: DecisionListener | None = None,
        channel: DecisionChannel | None = None,
    ) -> None:
        self._session = session
        self._repo = ApprovalRepository(session)
        self._listener = listener
        self._channel = channel

    async def get(self, approval_id: UUID) -> Approval | None:
        return await self._repo.get(approval_id)

    async def pending(self, run_id: UUID) -> list[Approval]:
        """刷新页面后恢复弹窗：直接查库，不依赖 Redis 里的事件还在。"""
        return await self._repo.list_pending(run_id)

    async def decide(
        self,
        approval_id: UUID,
        decision: Decision,
        *,
        by: UUID,
        mode: str | None = None,
        feedback: str | None = None,
        answers: list[dict[str, Any]] | None = None,
    ) -> Approval:
        """记下用户的决定，然后通知 Run 执行器续跑。

        拒绝**不终止 run** —— 智能体收到「用户拒绝」和理由作为工具结果，换个方案继续。
        ★ 只成功一次：条件更新只改 pending 的行，重复决定（两个标签页同时点）回冲突。
        """
        approval = await self._repo.get(approval_id)
        if approval is None:
            raise NotFound("确认项不存在", approval_id=str(approval_id))
        if approval.status != "pending":
            raise Conflict("该确认已被处理过或已超时", approval_id=str(approval_id))
        outcome = _decision_outcome(
            approval,
            decision=decision,
            approval_mode=mode,
            feedback=feedback,
            answers=answers,
        )
        if not await self._repo.decide(approval_id, decision=decision, user_id=by, outcome=outcome):
            raise Conflict("该确认已被处理过或已超时", approval_id=str(approval_id))
        await self._session.commit()
        # ★ 原生智能体的审批是挂起：run 落成 suspended 之后没有进程在等这个决定。
        #   不在这里通知的话要等定时补扫（默认 60s），用户点完「允许」得干等一分钟。
        #   通知是幂等的：放回队列有 CAS，与补扫撞上只有一个能赢。
        if self._listener is not None:
            await self._listener.decided(approval.run_uuid, approval_id)
        # CLI 的权限请求在线等这件事：叫醒它（可能在别的实例上）
        if self._channel is not None:
            await self._channel.publish(approval_id)
        return approval

    async def status(self, token: str) -> str:
        """续跑前核对：事项的状态；行丢了（或 token 非法）按 "failed"。

        ★ 记录丢了算「已结清」，不是「继续等」：等一个不存在的审批 = run 永久挂起。
        """
        try:
            row = await self._repo.get(UUID(token))
        except ValueError:
            logger.warning("waiting_on 里的 approval token 非法：%r", token)
            return "failed"
        return "failed" if row is None else row.status

    async def result(self, token: str) -> dict[str, Any]:
        """续跑交给图的审批结果（contracts/permission.py::ApprovalOutcome）。

        status 之外，计划审批带回用户选的执行模式，拒绝带回原因，提问带回答案。
        行丢了（或 token 非法）按 "failed"（门禁故障）。
        """
        try:
            row = await self._repo.get(UUID(token))
        except ValueError:
            logger.warning("waiting_on 里的 approval token 非法：%r", token)
            return {"status": "failed"}
        if row is None:
            return {"status": "failed"}
        extra = row.outcome if isinstance(row.outcome, dict) else {}
        picked = {k: extra[k] for k in ("mode", "feedback", "answers") if extra.get(k)}
        return {"status": row.status, **picked}

    async def expire_run(self, run_id: UUID) -> int:
        """run 进入终态：还挂着的事项一律 expired，弹窗随之消失。不提交，跟着调用方的事务。"""
        return await self._repo.expire_pending(run_id)


class RunApprovalGate:
    """注入给 engine 的审批门禁（ApprovalGate），一个 run 一个。

    engine 在工具执行前问它「这次批了吗」；它负责登记，然后**立刻**返回状态 ——
    等待由挂起机制承担。run 的状态不在这里动：原生智能体随后挂起（suspended），
    CLI 在线等权限由 CLI 运行时标 awaiting_user。
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
        """★ 不再有 timeout_s。

        挂起之后没有进程在等，审批也不判超时（只由人决定）：做了决定就在接到决定的
        实例上续跑，续跑信号丢了由执行器的兜底任务补查。gate 因此不需要知道时限。
        """
        self._sessionmaker = sessionmaker
        self._run_id = run_id

    async def check(
        self,
        *,
        approval_id: str,
        tool_name: str,
        args: dict[str, Any],
        kind: str = "tool",
        source: str | None = None,
        reason: str | None = None,
        tool_call_id: str | None = None,
        execution_modes: Sequence[str] | None = None,
    ) -> ApprovalState:
        """查一次状态。**立刻返回，不阻塞**。

        ★ 首次见到就登记，之后返回真实状态。「是不是首次」靠 approval_id 查库
          判断 —— 而 approval_id 由 (run_id, tool_call_id) 派生，跨段稳定
          （kernel 的 approval_id_for）。这是审批能挂起的前提：续跑时算出同一个
          id，于是查到上次那条记录而不是重新登记一遍。

        ★ 不再有 `_wait()`。原先那段短 BLPOP 累积到 600s 的逻辑整个删掉了 ——
          等待不占进程之后，「等多久」由恢复扫描判定（S8），而不是某个 await
          的超时分支。
        """
        aid = UUID(approval_id)
        try:
            async with self._sessionmaker() as session:
                approvals = ApprovalRepository(session)
                existing = await approvals.get(aid)
                if existing is not None:
                    return existing.status  # type: ignore[return-value]

                await approvals.create(
                    approval_id=aid,
                    run_id=self._run_id,
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=tool_call_id or _tool_call_id_of(args),
                    kind=kind,
                    source=source,
                    reason=reason,
                    execution_modes=list(execution_modes) if execution_modes is not None else None,
                )
                await session.commit()
        except Exception:
            # ★ 落库失败**不放行**。高风险工具无人把关地执行是这里最不能接受的
            #   失败方向（§12.2 的红线）—— 这一条没变。
            #
            # ★ 但返回的是 "failed" 而不是 "rejected"。两者的处理相同、含义
            #   相反：一个是系统正常工作的结果，一个是系统没工作。压成同一个值
            #   会让故障在界面上**完全隐形** —— 2026-09-23 一次 tool_name 超
            #   列宽被当成「用户拒绝」喂给 CLI，CLI 停手、子 run 零产出，父模型
            #   只能报「子智能体没有产出文本结论」，根因只在 Pod 日志里。
            #   调用方据此区分文案（contracts/approval.py 的 ApprovalState）。
            logger.exception("审批落库失败，按不放行处理 approval=%s", approval_id)
            return "failed"

        _tracer().start_span(
            "approval_requested",
            attributes={
                _sc.TOOL_NAME: tool_name,
                _sc.RUN_ID: str(self._run_id),
                "atlas.approval.id": approval_id,
            },
        ).end()
        return "pending"


class OnlineApprovals:
    """在线等待的审批：CLI 运行时的 ApprovalPort（code-cli-runtime-permission.html）。
    一个 run 一个。

    CLI 的权限请求进和原生同一张表（登记走 RunApprovalGate），但不能挂起：CLI 进程和 bridge
    都在等。所以多一个 wait —— 等决定信号，到点回库看一眼（信号丢了也能返回）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        channel: DecisionChannel,
        run_id: UUID,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._channel = channel
        self._gate = RunApprovalGate(sessionmaker, run_id)

    async def check(
        self,
        *,
        approval_id: str,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> ApprovalState:
        return await self._gate.check(
            approval_id=approval_id, tool_name=tool_name, args=args, tool_call_id=tool_call_id
        )

    async def wait(self, approval_id: str, timeout: float) -> str:
        """等这件事有结果，最多 timeout 秒。返回当时的状态（还没结果就是 pending）。

        ★ 先订阅、再回库看状态：看完之后才做的决定，信号一定收得到。
        """
        async with self._channel.listen(approval_id) as signal:
            state = await self._status(approval_id)
            if state != "pending":
                return state
            await signal.wait(timeout)
        return await self._status(approval_id)

    async def expire(self, approval_id: str) -> None:
        """撤回：bridge 已经给了 CLI 结果，这件事作废，前端的弹窗随之消失。"""
        async with self._sessionmaker() as session:
            await ApprovalRepository(session).expire(UUID(approval_id))
            await session.commit()

    async def _status(self, approval_id: str) -> str:
        async with self._sessionmaker() as session:
            return await ApprovalService(session).status(approval_id)


def _tool_call_id_of(args: dict[str, Any]) -> str | None:
    """从工具入参里尽力取出 toolCallId（acp 的 request_permission 带着它）。

    ★ 纯审计用途 —— 取不到就是 None。approval_id 的派生不依赖它（那用的是
      kernel 侧的 tool_call_id），这里只是让排查时少反推一步。
    """
    found = args.get("toolCallId") or args.get("tool_call_id")
    return str(found) if found else None


def _decision_outcome(
    approval: Approval,
    *,
    decision: Decision,
    approval_mode: str | None,
    feedback: str | None,
    answers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """决策带回的附加信息（approval.outcome），续跑时交给图（contracts/permission.py::ApprovalOutcome）。

    ★ 计划审批批准时必须带执行模式，且在登记时记下的范围里 —— 模式是权限，不能由请求随便指定。
    ★ 问题批准时校验答案并补上每题的来源；不带答案 = 全部按推荐
      （doc/ask-user-question-design.html D8），由这里**显式**写进 outcome ——
      审计看得到当时按什么继续的。
    """
    outcome: dict[str, Any] = {}
    if feedback and feedback.strip():
        outcome["feedback"] = feedback.strip()
    if approval.kind == "question" and decision == "approved":
        questions = (approval.args or {}).get("questions") or []
        try:
            outcome["answers"] = resolve_answers(questions, answers)
        except ValueError as exc:
            raise InvalidAnswers(str(exc)) from exc
    if approval.kind == "plan" and decision == "approved":
        allowed = list(approval.execution_modes or [])
        if approval_mode not in allowed:
            raise InvalidApprovalMode(
                "批准计划时要选一个执行模式，且必须在智能体允许的范围里",
                approval_mode=approval_mode,
                allowed=allowed,
            )
        outcome["mode"] = approval_mode
    return outcome
