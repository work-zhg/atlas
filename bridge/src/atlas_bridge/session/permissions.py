"""PermissionBroker：权限询问的上报、过期、撤回与兜底（Bridge 设计 §4.7 · §6.3；代码设计 §7.7）。

每个询问一个 Future：``handle`` 等它；server 答复、过期、取消、agent 撤回都只是「谁先 set_result」。

★ 绝不自动批准（B3）：本模块不存在生成 allow 结果的分支，只有「采用 server 的选择」一条路径
  能产生允许，而且 server 选的 optionId 必须在 agent 给出的选项里。
★ agent 的反向请求永远有回应（B6）：每条路径都以 set_result 收场。
★ 询问一律放进 Outbox。上游断开时由 Outbox 缓冲、重连后补发 —— 这就是 §7.2 的「暂存」；
  重连窗口到期后取消这一轮（cause: upstream_lost），询问随取消流程回 cancelled。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from atlas_host import (
    PermissionAnswer,
    PermissionAskParams,
    PermissionWithdrawParams,
    TurnState,
    methods,
)
from pydantic import ValidationError

from ..clock import Clock, Timer
from .ports import OutboxSink

if TYPE_CHECKING:
    from .turn import Turn

__all__ = ["PermissionBroker", "cancelled_result", "reject_result"]

logger = logging.getLogger(__name__)

WithdrawReason = Literal["turn_ended", "expired", "agent_withdrew"]


def cancelled_result() -> dict[str, Any]:
    """这一轮被取消时对所有挂起询问的答复（ACP 的取消义务，Bridge 设计 §6.4 ②）。"""
    return {"outcome": {"outcome": "cancelled"}}


def selected_result(option_id: str) -> dict[str, Any]:
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _options(request: Any) -> list[dict[str, Any]]:
    options = request.get("options") if isinstance(request, dict) else None
    if not isinstance(options, list):
        return []
    return [o for o in options if isinstance(o, dict) and isinstance(o.get("optionId"), str)]


def reject_result(request: Any) -> dict[str, Any]:
    """拒绝：选 agent 给出的 ``reject_once``；没有则 ``reject_always``；都没有则回 cancelled。"""
    options = _options(request)
    for kind in ("reject_once", "reject_always"):
        for option in options:
            if option.get("kind") == kind:
                return selected_result(option["optionId"])
    return cancelled_result()


@dataclass(eq=False)
class Ask:
    ask_id: int
    turn: Turn
    request: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    timer: Timer | None = None
    settled: bool = field(default=False)


class PermissionBroker:
    def __init__(self, outbox: OutboxSink, clock: Clock, *, max_pending: int = 8) -> None:
        self._outbox = outbox
        self._clock = clock
        self._max_pending = max_pending
        #: ask_id = 上游 JSON-RPC 请求的 id，跨连接不变（补发时沿用，§7.3）
        self._ids = itertools.count(1)
        self.asks: dict[int, Ask] = {}

    def pending(self, turn: Turn) -> int:
        return sum(1 for a in self.asks.values() if a.turn is turn)

    # ------------------------------------------------------------------ 来自 agent

    async def handle(self, turn: Turn | None, request: dict[str, Any]) -> dict[str, Any]:
        """agent 的 session/request_permission → 返回 ACP 的 RequestPermissionResponse。"""
        if turn is None:
            # 没有进行中的一轮（v2 的后台活动）：没有人会看到这次询问
            return reject_result(request)
        if turn.state in (TurnState.CANCELLING, TurnState.ENDED):
            return cancelled_result()  # §6.4 ④：取消期间的新询问直接回 cancelled，不上报
        if self.pending(turn) >= self._max_pending:
            logger.warning("挂起的权限询问超过 %d 个，直接拒绝", self._max_pending)
            return reject_result(request)

        loop = asyncio.get_running_loop()
        ask = Ask(next(self._ids), turn, request, loop.create_future())
        self.asks[ask.ask_id] = ask

        # ★ 默认两者都是 None：审批只由人决定，一直等到答复（或这一轮被取消、结束）
        wait_s = turn.limits.permission_wait_s
        deadline_left = turn.deadline_remaining
        bounds = [v for v in (wait_s, deadline_left) if v is not None]
        expires_in = min(bounds) if bounds else None
        if wait_s is not None and (deadline_left is None or wait_s < deadline_left):
            # 截止先到时不设询问计时：截止触发取消，询问随之回 cancelled（§6.3）
            ask.timer = Timer(self._clock, lambda: self._expire(ask), name=f"ask-{ask.ask_id}")
            ask.timer.start(wait_s)

        turn.on_permission_pending(self.pending(turn))  # → awaiting_permission
        expires_at = (
            None if expires_in is None else datetime.now(UTC) + timedelta(seconds=expires_in)
        )
        self._outbox.put_request(
            methods.PERMISSION_ASK,
            lambda seq: PermissionAskParams(
                seq=seq,
                turn_id=turn.turn_id,
                request=request,
                expires_at=expires_at,
                expires_in_s=expires_in,
            ),
            request_id=ask.ask_id,
        )
        try:
            return await ask.future
        except asyncio.CancelledError:
            # agent 撤回了请求，或 agent 通道关闭：告诉 server 关掉审批界面
            self._settle(ask, None, withdraw="agent_withdrew")
            raise

    # ------------------------------------------------------------------ 来自 server

    def on_answer(self, ask_id: int, result: Any = None, error: Any = None) -> None:
        """server 对 permission.ask 的响应。已有结果的询问（撤回之后才到的答复）忽略。"""
        ask = self.asks.get(ask_id)
        if ask is None:
            logger.info("忽略询问 %d 的迟到答复", ask_id)
            return
        self._settle(ask, self._translate(ask, result, error))

    def _translate(self, ask: Ask, result: Any, error: Any) -> dict[str, Any]:
        if error is not None:
            logger.warning("server 对询问 %d 回了错误，按拒绝处理：%s", ask.ask_id, error)
            return reject_result(ask.request)
        try:
            answer = PermissionAnswer.model_validate(result)
        except ValidationError:
            logger.warning("询问 %d 的答复形状不对，按拒绝处理", ask.ask_id)
            return reject_result(ask.request)
        if answer.option_id is None:
            return reject_result(ask.request)
        if all(o["optionId"] != answer.option_id for o in _options(ask.request)):
            logger.warning("server 选了不存在的选项 %r，按拒绝处理", answer.option_id)
            return reject_result(ask.request)
        return selected_result(answer.option_id)

    # ------------------------------------------------------------------ 兜底

    def settle_all(self, turn: Turn) -> None:
        """这一轮被取消或已结束：挂起的询问全部回 cancelled，并向 server 撤回（§6.4 ②）。

        不再通知 Turn 挂起数的变化 —— 它正在取消或结束，不会回到 running。
        """
        for ask in [a for a in self.asks.values() if a.turn is turn]:
            self._settle(ask, cancelled_result(), withdraw="turn_ended", notify_turn=False)

    def _expire(self, ask: Ask) -> None:
        """到期未答复：给 agent 选拒绝，向 server 撤回，轮次回到 running（§6.3）。"""
        self._settle(ask, reject_result(ask.request), withdraw="expired")

    def _settle(
        self,
        ask: Ask,
        result: dict[str, Any] | None,
        *,
        withdraw: WithdrawReason | None = None,
        notify_turn: bool = True,
    ) -> None:
        if ask.settled:
            return
        ask.settled = True
        del self.asks[ask.ask_id]
        if ask.timer is not None:
            ask.timer.stop()
        if result is not None and not ask.future.done():
            ask.future.set_result(result)
        if withdraw is not None:
            self._outbox.put_notification(
                methods.PERMISSION_WITHDRAW,
                lambda seq: PermissionWithdrawParams(seq=seq, ask_id=ask.ask_id, reason=withdraw),
                kind="control",
            )
        if notify_turn:
            ask.turn.on_permission_pending(self.pending(ask.turn))
