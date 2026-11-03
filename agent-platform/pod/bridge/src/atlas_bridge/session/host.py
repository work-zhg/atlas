"""SessionHost：会话状态机，上游指令的唯一入口（Bridge 设计 §5.5 · 代码设计 §7.2）。

★ 所有状态只在事件循环里修改，没有锁。一次状态转换在同一个同步段里完成「改状态 + 放入消息」；
  需要 await 的操作先转到中间态（OPENING、CLOSING），醒来后先检查状态是否已被别的事件改变。
★ 每个上游方法都有允许的状态，不在其中就抛 SessionNotOpen 等异常，由 dispatch 转成错误码。
★ agent 进程不可用（意外退出、取消不响应、ACP 请求超时）时，唯一的收场是重启它（§6.5）：
  会话未打开 → 重启后回到 IDLE；会话已打开 → RECOVERING，重启并恢复同一个 agentSessionId，
  结果以 session.state 告诉 server（§5.6）。超过重启节制则 session.ended。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from enum import StrEnum
from typing import Any

from atlas_host import (
    DEFAULT_LIMITS,
    AcpNegotiation,
    AgentRestart,
    AgentUpdateParams,
    BridgeInfo,
    CancelCause,
    ErrorInfo,
    FailCause,
    Failed,
    ModeState,
    OpenParams,
    OpenResult,
    ResumeSpec,
    SessionEndedParams,
    SessionState,
    SessionStateParams,
    TurnCancelResult,
    TurnLimits,
    TurnRef,
    TurnStartParams,
    TurnStartResult,
    TurnState,
    TurnStateParams,
    methods,
)
from atlas_jsonrpc import ChannelClosed

from ..cli.ports import CliPort
from ..clock import Clock
from ..errors import (
    AcpRequestTimeout,
    AgentRequestFailed,
    AgentUnavailable,
    SessionAlreadyOpen,
    SessionNotOpen,
    TurnBusy,
    TurnNotFound,
)
from ..permission.broker import PermissionBroker
from ..turn.turn import Turn
from ..upstream.ports import UpstreamOutbox
from .keeper import CliKeeper
from .ledger import TurnLedger, TurnRecord
from .modes import ModeGuard

__all__ = ["Phase", "SessionHost"]

logger = logging.getLogger(__name__)

#: agent 以这两种方式失败后，进程状态不可信（Bridge 设计 §6.4 ⑥ · §6.5）
_AGENT_BROKEN = frozenset({FailCause.AGENT_EXITED, FailCause.CANCEL_UNANSWERED})


class Phase(StrEnum):
    """bridge 内部的会话状态（§5.5）。server 可见的是更粗的 atlas_host.SessionState。"""

    BOOTING = "booting"
    IDLE = "idle"
    OPENING = "opening"
    READY = "ready"
    IN_TURN = "in_turn"
    RECOVERING = "recovering"
    CLOSING = "closing"
    ENDED = "ended"


class SessionHost:
    def __init__(
        self,
        agent: CliPort,
        outbox: UpstreamOutbox,
        clock: Clock,
        *,
        bridge: BridgeInfo,
        workspace: str,
        fallback_limits: TurnLimits = DEFAULT_LIMITS,
        open_timeout_s: float = 120,
        request_timeout_s: float = 30,
        cancel_grace_s: float = 15,
        ledger_size: int = 16,
        max_pending_asks: int = 8,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._bridge = bridge
        self._fallback = fallback_limits
        self._request_timeout_s = request_timeout_s
        self._cancel_grace_s = cancel_grace_s

        self.phase = Phase.BOOTING
        self.agent_session_id: str | None = None
        #: open 时确定的会话默认时限（server 给的 defaults 覆盖兜底值）
        self.limits: TurnLimits = fallback_limits
        self.turn: Turn | None = None
        self.ledger = TurnLedger(ledger_size)
        self.permissions = PermissionBroker(outbox, clock, max_pending=max_pending_asks)
        #: 会话结束（不论谁发起）时 set；上游层据此以 4410 关闭连接，bridge 随之退出
        self.ended = asyncio.Event()
        #: bridge 自己结束会话的原因（如 agent_crash_loop）；server 要求的关闭、排空为 None
        self.end_cause: str | None = None

        self._tasks: set[asyncio.Task[Any]] = set()
        #: CLI 的权限模式：每轮按 server 的要求设上，恢复会话后重新设上
        self.modes = ModeGuard(agent, request_timeout_s=request_timeout_s)
        #: CLI 这一侧：预热、新建 / 恢复 / 关闭会话、重启
        self._keeper = CliKeeper(
            agent,
            self.modes,
            workspace=workspace,
            open_timeout_s=open_timeout_s,
            request_timeout_s=request_timeout_s,
        )
        #: turn.start 要先 await 设置模式：串行化，免得两个 turn.start 同时通过「没有进行中的
        #: 一轮」这道检查
        self._start_lock = asyncio.Lock()
        agent.bind(self)

    # ═══════════════════════════════════ 启动 ═══════════════════════════════════

    async def boot(self) -> None:
        """预热：拉起 agent 并完成 initialize（§5.5）。BOOTING → IDLE。"""
        assert self.phase is Phase.BOOTING
        await self._keeper.boot()
        if self.phase is Phase.BOOTING:
            self.phase = Phase.IDLE

    # ═══════════════════════════════════ 上游指令 ═══════════════════════════════════

    async def open(self, params: OpenParams) -> OpenResult:
        """IDLE → OPENING → READY。重放的历史在响应之前以 agent.update(replay) 送出。"""
        if self.phase is Phase.BOOTING:
            await self._keeper.booted.wait()  # 预热还没结束：等它（§6.5）
            if self.phase is Phase.BOOTING:
                raise AgentUnavailable("boot_failed")
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            raise SessionNotOpen("ended")
        if self.phase is not Phase.IDLE:
            raise SessionAlreadyOpen(self._bridge.instance, self.agent_session_id)

        self.phase = Phase.OPENING
        self.limits = params.defaults.over(self._fallback)
        try:
            session_id, resumed, replayed = await self._keeper.open_session(
                params.resume, params.mcp_servers
            )
        except AcpRequestTimeout:
            # agent 可能仍在处理（比如还在重放历史）：重启它，回到 IDLE，server 可以重试（§6.5）
            if self.phase is Phase.OPENING:
                self._agent_broken("open_timeout")
            raise
        except ChannelClosed as exc:
            if self.phase is Phase.OPENING:
                self.phase = Phase.IDLE  # 退出监视随后会重启 agent
            raise AgentUnavailable("agent_exited") from exc
        except BaseException:
            if self.phase is Phase.OPENING:
                self.phase = Phase.IDLE
            raise
        if self.phase is not Phase.OPENING:  # 打开期间 agent 失联，会话已结束
            raise SessionNotOpen("ended")
        self.agent_session_id = session_id
        self.phase = Phase.READY

        negotiated = self._keeper.negotiated
        return OpenResult(
            agent_session_id=session_id,
            resumed=resumed,
            replayed=replayed,
            acp=AcpNegotiation(
                protocol_version=negotiated.protocol_version,
                agent_info=negotiated.agent_info,
                agent_capabilities=negotiated.agent_capabilities,
            ),
            bridge=self._bridge,
        )

    async def start_turn(self, params: TurnStartParams) -> TurnStartResult:
        """READY → IN_TURN。同一个 turnId 重复提交是安全的（§4.6）。"""
        async with self._start_lock:
            return await self._start_turn(params)

    async def _start_turn(self, params: TurnStartParams) -> TurnStartResult:
        record = self.ledger.get(params.turn_id)
        if record is not None:  # 已结束的轮次：重发一次它的 ended
            self._emit_turn_ended(params.turn_id, record)
            return TurnStartResult()
        if self.turn is not None:
            if self.turn.turn_id == params.turn_id:
                return TurnStartResult()
            raise TurnBusy(self.turn.turn_id)
        self._require_open()
        assert self.agent_session_id is not None

        mode: ModeState | None = None
        if params.mode is not None:
            mode = await self.modes.apply(self.agent_session_id, params.mode)
            # 设置模式要 await：期间 agent 可能退出（→ RECOVERING），或会话被关闭
            self._require_open()
            assert self.agent_session_id is not None

        limits = params.limits.over(self.limits) if params.limits else self.limits
        turn = Turn(
            params.turn_id,
            prompt=params.prompt,
            trace=params.trace,
            limits=limits,
            agent_session_id=self.agent_session_id,
            client=self._keeper.client,
            outbox=self._outbox,
            clock=self._clock,
            owner=self,
            cancel_grace_s=self._cancel_grace_s,
            mode=mode,
        )
        self.turn = turn
        self.phase = Phase.IN_TURN
        if mode is not None and ModeGuard.too_permissive(mode):
            detail = f"要求的权限模式 {mode.requested} 设置不上，实际是更宽松的 {mode.effective}"
            error = ErrorInfo(cause="mode_unavailable", detail=detail)
            turn.reject(FailCause.MODE_UNAVAILABLE, error)
            return TurnStartResult()
        await turn.start()
        return TurnStartResult()

    def cancel_turn(self, turn_id: str) -> TurnCancelResult:
        """响应只表示「已开始取消」；取消的结果以随后的 turn.state(ended) 为准。"""
        if self.turn is not None and self.turn.turn_id == turn_id:
            self.turn.begin_cancel(CancelCause.REQUESTED)
            return TurnCancelResult(state="cancelling")
        if turn_id in self.ledger:
            return TurnCancelResult(state="ended")
        raise TurnNotFound(turn_id)

    def readiness(self) -> tuple[bool, str]:
        """/readyz：CLI 完成初始化就算就绪；会话在收尾或已结束就不再就绪（进程随后退出）。"""
        ready = self.phase not in (Phase.BOOTING, Phase.CLOSING, Phase.ENDED)
        return ready, self.phase.value

    def snapshot(self, bridge_instance: str) -> tuple[SessionState, TurnRef | None]:
        """session.attach：会话此刻的状态和当前一轮（§7.3）。补发计划由上游通道按 Outbox 算。

        ★ server 的判断依据是 state 与 turn，不是补发的消息：会话此刻处于什么状态以这里为准。
        """
        if bridge_instance != self._bridge.instance:
            # bridge 重启过，进程内状态全丢；server 据此让进行中的一轮失败并 session.open 恢复
            raise SessionNotOpen("bridge_restarted", bridgeInstance=self._bridge.instance)
        state = {
            Phase.READY: SessionState.READY,
            Phase.IN_TURN: SessionState.IN_TURN,
            Phase.RECOVERING: SessionState.RECOVERING,
            Phase.CLOSING: SessionState.ENDED,
            Phase.ENDED: SessionState.ENDED,
        }.get(self.phase)
        if state is None:
            raise SessionNotOpen("not_opened")
        turn = self.turn
        return state, (
            TurnRef(turn_id=turn.turn_id, state=turn.state) if turn and turn.state else None
        )

    def upstream_changed(self, connected: bool) -> None:
        """上游连接建立 / 断开（由 UpstreamServer 调用）。断开不结束这一轮（§7.2）；
        重连窗口的计时在一轮里。"""
        if self.turn is not None:
            self.turn.on_upstream(connected)

    async def close(self) -> None:
        """session.close：若有一轮在进行，先按取消流程结束它；再关闭 CLI 会话、终止 CLI。幂等。"""
        await self._wind_down(CancelCause.SESSION_CLOSING)

    async def drain(self) -> None:
        """SIGTERM：排空。拒绝新的一轮（进入 CLOSING 后 turn.start 回 session_not_open），当前一轮
        以 pod_terminating 取消、结束状态送出之后再关闭 CLI —— server 能给出明确的结果，不用等断线
        重连、attach 得到 bridge_restarted 才知道这一轮没了。幂等。"""
        await self._wind_down(CancelCause.POD_TERMINATING)

    async def _wind_down(self, cause: CancelCause) -> None:
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            await self.ended.wait()
            return
        self.phase = Phase.CLOSING
        turn = self.turn
        if turn is not None:
            turn.begin_cancel(cause)
            await turn.done.wait()
        if self.agent_session_id is not None:
            await self._keeper.close_session(self.agent_session_id)
        await self._keeper.shutdown()
        self.phase = Phase.ENDED
        self.ended.set()

    def answer_permission(self, ask_id: int, result: Any = None, error: Any = None) -> None:
        """server 对 permission.ask 的响应（由上游连接按 id 转交）。"""
        self.permissions.on_answer(ask_id, result, error)

    # ═══════════════════════════════════ 来自 agent ═══════════════════════════════════

    async def on_update(self, raw: dict[str, Any]) -> None:
        """定 origin → Outbox；属于当前一轮的，再交给 Turn 读取进展信号。

        ★ 先等 Outbox 可写：缓冲满时这里停下，AcpClient 的读循环随之停下，压力传回 agent（§6.6）。
        """
        self.modes.observe(raw)
        await self._outbox.wait_writable()
        if self.phase is Phase.ENDED:
            return
        turn = self.turn
        if self._keeper.replaying:
            origin, turn_id = "replay", None
        elif turn is not None:
            origin, turn_id = "turn", turn.turn_id
        else:
            origin, turn_id = "stray", None
        self._outbox.put_notification(
            methods.AGENT_UPDATE,
            lambda seq: AgentUpdateParams(seq=seq, origin=origin, turn_id=turn_id, update=raw),
            kind="data",
        )
        if turn is not None:
            turn.on_update(raw)

    async def on_permission_request(self, raw: dict[str, Any]) -> dict[str, Any]:
        """B6：agent 的反向请求永远有回应（由 PermissionBroker 保证）。"""
        return await self.permissions.handle(self.turn, raw)

    def on_cli_lost(self, cause: FailCause) -> None:
        """agent 进程退出。关闭过程中的退出是预期内的。"""
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            return
        self._keeper.healthy = False
        if self.turn is not None:
            self.turn.fail(cause)  # → turn_ended → _agent_broken
        else:
            self._agent_broken(cause.value)

    # ═══════════════════════════════════ TurnOwner ═══════════════════════════════════

    def settle_permissions(self, turn_id: str) -> None:
        self.permissions.settle_all(turn_id)

    def turn_ended(self, turn: Turn) -> None:
        assert turn.outcome is not None and turn.stats is not None
        self.ledger.record(turn.turn_id, TurnRecord(turn.outcome, turn.stats))
        if self.turn is turn:
            self.turn = None
        if self.phase is Phase.IN_TURN:
            self.phase = Phase.READY
        outcome = turn.outcome
        if isinstance(outcome, Failed) and outcome.cause in _AGENT_BROKEN:
            self._keeper.healthy = False
            self._agent_broken(outcome.cause.value)

    # ═══════════════════════════════════ 内部 ═══════════════════════════════════

    def _require_open(self) -> None:
        match self.phase:
            case Phase.READY:
                return
            case Phase.RECOVERING:
                raise SessionNotOpen("recovering")
            case Phase.CLOSING | Phase.ENDED:
                raise SessionNotOpen("ended")
            case _:
                raise SessionNotOpen("not_opened")

    def _agent_broken(self, cause: str) -> None:
        """agent 已不可用：重启它。已在重启中、正在关闭时不重复处理。"""
        self._keeper.healthy = False
        match self.phase:
            case Phase.IDLE | Phase.OPENING:
                self.phase = Phase.BOOTING
                self._keeper.booted.clear()
                self._spawn(self._reboot(cause))
            case Phase.READY | Phase.IN_TURN:
                self.phase = Phase.RECOVERING
                restarts = self._keeper.restarts + 1
                self._outbox.put_notification(
                    methods.SESSION_STATE,
                    lambda seq: SessionStateParams(
                        seq=seq,
                        state="recovering",
                        agent=AgentRestart(restarts=restarts, cause=cause),
                    ),
                    kind="control",
                )
                self._spawn(self._recover(cause))
            case _:  # BOOTING · RECOVERING：已在重启；CLOSING · ENDED：随后就终止
                pass

    async def _reboot(self, cause: str) -> None:
        """会话尚未打开时 agent 不可用：重启后回到 IDLE。"""
        try:
            await self._keeper.restart(cause)
        except AgentUnavailable as exc:
            if self.phase is Phase.BOOTING:
                self._end(exc.cause, ErrorInfo(cause=exc.cause))
            return
        if self.phase is Phase.BOOTING:
            self._keeper.healthy = True
            self.phase = Phase.IDLE

    async def _recover(self, cause: str) -> None:
        """RECOVERING：重启 agent，恢复同一个会话，不重放（§5.6）。"""
        previous = self.agent_session_id
        assert previous is not None
        while True:
            try:
                await self._keeper.restart(cause)
            except AgentUnavailable as exc:
                if self.phase is Phase.RECOVERING:
                    self._end(exc.cause, ErrorInfo(cause=exc.cause))
                return
            if self.phase is not Phase.RECOVERING:
                return
            try:
                session_id, resumed, _ = await self._keeper.open_session(
                    ResumeSpec(agent_session_id=previous, replay="none")
                )
                break
            except (AcpRequestTimeout, AgentRequestFailed, ChannelClosed) as exc:
                logger.warning("恢复会话失败，再次重启 agent：%s", exc)
                cause = "recover_failed"
        if self.phase is not Phase.RECOVERING:
            return
        self.agent_session_id = session_id
        # ★ 恢复出来的会话模式被重置了：把 server 要的那个重新应用上，否则这一轮之后
        #   直到下一个 turn.start 之前，agent 都以默认模式运行
        mode = await self.modes.reapply(session_id)
        if mode is not None and self.phase is not Phase.RECOVERING:
            return
        self._keeper.healthy = True
        self.phase = Phase.READY
        restart = AgentRestart(
            restarts=self._keeper.restarts, resumed=resumed, agent_session_id=session_id, mode=mode
        )
        self._outbox.put_notification(
            methods.SESSION_STATE,
            lambda seq: SessionStateParams(seq=seq, state="ready", agent=restart),
            kind="control",
        )

    def _end(self, cause: str, error: ErrorInfo | None = None) -> None:
        """会话在 server 没有要求的情况下结束：发 session.ended。"""
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            return
        self.phase = Phase.ENDED
        self.end_cause = cause
        self._outbox.put_notification(
            methods.SESSION_ENDED,
            lambda seq: SessionEndedParams(seq=seq, cause=cause, error=error),
            kind="control",
        )
        self.ended.set()

    def _emit_turn_ended(self, turn_id: str, record: TurnRecord) -> None:
        self._outbox.put_notification(
            methods.TURN_STATE,
            lambda seq: TurnStateParams(
                seq=seq,
                turn_id=turn_id,
                state=TurnState.ENDED,
                outcome=record.outcome,
                stats=record.stats,
            ),
            kind="control",
        )

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
