"""Turn：一轮的状态机、计时与取消流程（Bridge 设计 §4.6 · §5.4 · §6.2 · §6.4；代码设计 §7.5）。

一轮 = 恰好一次 ``session/prompt``（v1）：发出即开始，它的响应（结果或错误）即结束。

★ ``_finish`` 是唯一写入 ENDED 的方法，重复进入直接返回 —— B1「结束只宣告一次」由这一处保证。
  取消与完成同时发生时，先到的生效，后到的是空操作。
★ 取消期间 agent 以非 cancelled 的原因结束 → completed：以 agent 的实际结果为准（§6.4）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any, Protocol

from atlas_host import (
    CancelCause,
    Cancelled,
    Completed,
    ErrorInfo,
    FailCause,
    Failed,
    ModeState,
    Outcome,
    TurnLimits,
    TurnState,
    TurnStateParams,
    TurnStats,
    methods,
)
from atlas_jsonrpc import ChannelClosed, RpcFault

from ..agent.client import AcpClient
from ..clock import Clock, Timer
from .ports import OutboxSink
from .progress import ProgressTracker

__all__ = ["Turn", "TurnOwner"]

logger = logging.getLogger(__name__)


class TurnOwner(Protocol):
    """Turn 需要通知持有者的两件事。由 HostSession 实现。"""

    def settle_permissions(self, turn: Turn) -> None:
        """进入取消或即将结束：挂起的权限询问全部回 cancelled 并撤回（§6.4 ②）。"""

    def turn_ended(self, turn: Turn) -> None:
        """这一轮已结束（turn.state ended 已放入 Outbox）。"""


class Turn:
    def __init__(
        self,
        turn_id: str,
        *,
        prompt: list[dict[str, Any]],
        trace: str | None,
        limits: TurnLimits,
        agent_session_id: str,
        client: AcpClient,
        outbox: OutboxSink,
        clock: Clock,
        owner: TurnOwner,
        cancel_grace_s: float,
        mode: ModeState | None = None,
    ) -> None:
        assert limits.is_complete(), "Turn 需要合并后的静默阈值"
        self.turn_id = turn_id
        self._prompt = prompt
        self._trace = trace
        self._limits = limits
        self._session_id = agent_session_id
        self._client = client
        self._outbox = outbox
        self._clock = clock
        self._owner = owner
        self._cancel_grace_s = cancel_grace_s
        #: 本轮实际生效的权限模式，随第一个 running 报给 server
        self._mode = mode

        #: None = 还没开始
        self.state: TurnState | None = None
        self.outcome: Outcome | None = None
        self.stats: TurnStats | None = None
        self.cancel_cause: CancelCause | None = None
        self.progress = ProgressTracker(idle_s=limits.idle_s, tool_idle_s=limits.tool_idle_s)  # type: ignore[arg-type]
        #: 结束时 set，供 session.close 等待
        self.done = asyncio.Event()

        # 三个计时器：截止（可选，不可暂停）、静默（等人时暂停、有进展时重置）、取消宽限
        self._deadline = Timer(clock, self._on_deadline, name=f"{turn_id}:deadline")
        self._idle = Timer(clock, self._on_idle, name=f"{turn_id}:idle")
        self._grace = Timer(clock, self._on_cancel_grace, name=f"{turn_id}:cancel-grace")

        self._started_at = 0.0
        self._awaiting_since: float | None = None
        self._awaiting_total = 0.0
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def ended(self) -> bool:
        return self.state is TurnState.ENDED

    @property
    def limits(self) -> TurnLimits:
        return self._limits

    @property
    def deadline_remaining(self) -> float | None:
        return self._deadline.remaining

    # ------------------------------------------------------------------ 开始

    async def start(self) -> None:
        """发出 session/prompt。返回时 prompt 已交给 agent（turn.start 的「已接纳」）。"""
        assert self.state is None, "一轮只能开始一次"
        self.state = TurnState.RUNNING
        self._started_at = self._clock.now()
        self._emit_state(mode=self._mode)
        # 截止从把 prompt 交给 agent 的那一刻算起（§6.2）。默认不设：CLI 在干活就不中断
        if self._limits.deadline_s is not None:
            self._deadline.start(self._limits.deadline_s)
        self._idle.start(self.progress.idle_threshold)
        try:
            future = await self._client.start_prompt(
                self._session_id, self._prompt, trace=self._trace
            )
        except ChannelClosed:
            self._finish(Failed(cause=FailCause.AGENT_EXITED))
            return
        future.add_done_callback(self._on_prompt_done)

    # ------------------------------------------------------------------ 来自 agent 的信号

    def on_update(self, raw: dict[str, Any]) -> None:
        """这一轮的一条 update：进展证据。等人时静默计时暂停，不在这里重置。"""
        if self.ended:
            return
        self.progress.observe(raw)
        if self.state is TurnState.RUNNING:
            self._idle.reset(self.progress.idle_threshold)

    def on_permission_pending(self, pending: int) -> None:
        """挂起的询问数变化：0 → >0 进入等人，>0 → 0 回到 running（§6.3）。"""
        if self.state is TurnState.RUNNING and pending > 0:
            self.state = TurnState.AWAITING_PERMISSION
            self._awaiting_since = self._clock.now()
            self._idle.pause()
            self._emit_state()
        elif self.state is TurnState.AWAITING_PERMISSION and pending == 0:
            self._stop_awaiting()
            self.state = TurnState.RUNNING
            self._idle.reset(self.progress.idle_threshold)
            self._emit_state()

    def reject(self, cause: FailCause, error: ErrorInfo | None = None) -> None:
        """还没发出 prompt 就判失败（例如要求的权限模式设置不上）。只宣告一次 ended。"""
        assert self.state is None, "只能在开始之前拒绝"
        self._started_at = self._clock.now()
        self._finish(Failed(cause=cause, error=error))

    def fail(self, cause: FailCause, error: ErrorInfo | None = None) -> None:
        """由外部判定这一轮失败（agent 进程退出）。"""
        self._finish(Failed(cause=cause, error=error))

    # ------------------------------------------------------------------ 取消（§6.4）

    def begin_cancel(self, cause: CancelCause) -> None:
        """①–④。幂等：已在取消或已结束时是空操作。"""
        if self.state in (None, TurnState.CANCELLING, TurnState.ENDED):
            return
        self._stop_awaiting()
        self.state = TurnState.CANCELLING
        self.cancel_cause = cause
        self._deadline.stop()
        self._idle.stop()
        self._emit_state()  # ①
        self._owner.settle_permissions(self)  # ② 挂起的询问全部回 cancelled
        self._spawn(self._client.cancel(self._session_id))  # ③
        self._grace.start(self._cancel_grace_s)  # ④

    def _on_deadline(self) -> None:
        self.begin_cancel(CancelCause.DEADLINE)

    def _on_idle(self) -> None:
        self.begin_cancel(CancelCause.IDLE)

    def _on_cancel_grace(self) -> None:
        """⑥ 宽限到点仍无结果：只能终止 agent（由持有者在 turn_ended 里执行）。"""
        self._finish(Failed(cause=FailCause.CANCEL_UNANSWERED))

    # ------------------------------------------------------------------ 结束

    def _on_prompt_done(self, future: asyncio.Future[Any]) -> None:
        if self.ended:
            return
        if future.cancelled():
            self._finish(Failed(cause=FailCause.AGENT_EXITED))
            return
        exc = future.exception()
        if isinstance(exc, RpcFault):
            if self.state is TurnState.CANCELLING:
                # 违反规范（应以 cancelled 结束），但取消的目的已达到
                logger.warning("agent 取消时回了错误：%s", exc, extra={"turn": self.turn_id})
                self._finish(Cancelled(cause=self.cancel_cause))  # type: ignore[arg-type]
            else:
                error = ErrorInfo(cause=FailCause.AGENT_ERROR, acp=exc.to_json())
                self._finish(Failed(cause=FailCause.AGENT_ERROR, error=error))
            return
        if exc is not None:  # 通道关闭：agent 退出了
            self._finish(Failed(cause=FailCause.AGENT_EXITED))
            return
        result = future.result()
        stop = result.get("stopReason") if isinstance(result, dict) else None
        if not isinstance(stop, str):
            error = ErrorInfo(cause="invalid_response", detail="prompt 响应缺少 stopReason")
            self._finish(Failed(cause=FailCause.AGENT_ERROR, error=error))
        elif stop == "cancelled" and self.state is TurnState.CANCELLING:
            self._finish(Cancelled(cause=self.cancel_cause, response=result))  # type: ignore[arg-type]
        else:
            self._finish(Completed(stop_reason=stop, response=result))

    def _finish(self, outcome: Outcome) -> None:
        """唯一写入 ENDED 的地方（B1）。"""
        if self.ended:
            return
        self._stop_awaiting()
        for timer in (self._deadline, self._idle, self._grace):
            timer.stop()
        # 不经取消直接结束（agent 退出）时仍可能有挂起的询问；撤回排在 ended 之前
        self._owner.settle_permissions(self)
        self.state = TurnState.ENDED
        self.outcome = outcome
        self.stats = TurnStats(
            elapsed_s=max(0.0, self._clock.now() - self._started_at),
            awaiting_permission_s=self._awaiting_total,
        )
        self._emit_state()
        self.done.set()
        self._owner.turn_ended(self)

    # ------------------------------------------------------------------ 内部

    def _stop_awaiting(self) -> None:
        if self._awaiting_since is not None:
            self._awaiting_total += self._clock.now() - self._awaiting_since
            self._awaiting_since = None

    def _emit_state(self, mode: ModeState | None = None) -> None:
        state, outcome, stats = self.state, self.outcome, self.stats
        assert state is not None
        self._outbox.put_notification(
            methods.TURN_STATE,
            lambda seq: TurnStateParams(
                seq=seq, turn_id=self.turn_id, state=state, outcome=outcome, stats=stats, mode=mode
            ),
            kind="control",
        )

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(self._quiet(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _quiet(coro: Coroutine[Any, Any, Any]) -> None:
        # session/cancel 是通知：通道已关闭说明 agent 退出了，由退出监视处理
        with contextlib.suppress(ChannelClosed):
            await coro
