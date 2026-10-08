"""HostSession：会话状态机，上游指令的唯一入口（Bridge 设计 §5.5 · 代码设计 §7.2）。

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
    AttachParams,
    AttachResult,
    BridgeInfo,
    CancelCause,
    ErrorInfo,
    FailCause,
    Failed,
    JsonObject,
    ModeState,
    OpenParams,
    OpenResult,
    ResumeSpec,
    SeqRange,
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

from ..agent.resume import OpenMethod, plan_open
from ..clock import Clock, Timer
from ..errors import (
    AcpRequestTimeout,
    AgentRequestFailed,
    AgentUnavailable,
    SessionAlreadyOpen,
    SessionNotOpen,
    TurnBusy,
    TurnNotFound,
)
from .ledger import TurnLedger, TurnRecord
from .permissions import PermissionBroker
from .ports import AgentPort, OutboxSink
from .turn import Turn

__all__ = ["HostSession", "Phase"]

logger = logging.getLogger(__name__)

#: agent 以这两种方式失败后，进程状态不可信（Bridge 设计 §6.4 ⑥ · §6.5）
_AGENT_BROKEN = frozenset({FailCause.AGENT_EXITED, FailCause.CANCEL_UNANSWERED})

#: 权限模式的宽松程度（小 = 保守）。要求的模式设不上时，据此判断「能不能以实际的模式跑」：
#: 实际的比要求的更宽松 → 不跑（宁可失败，也不以超出预期的权限执行）。不认识的模式按中间算。
_MODE_RANK = {
    "plan": 0,
    "dontAsk": 0,
    "default": 1,
    "acceptEdits": 2,
    "auto": 3,
    "bypassPermissions": 4,
}


def _rank(mode: str) -> int:
    return _MODE_RANK.get(mode, 2)


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


class HostSession:
    def __init__(
        self,
        agent: AgentPort,
        outbox: OutboxSink,
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
        self._agent = agent
        self._outbox = outbox
        self._clock = clock
        self._bridge = bridge
        self._workspace = workspace
        self._fallback = fallback_limits
        self._open_timeout_s = open_timeout_s
        self._request_timeout_s = request_timeout_s
        self._cancel_grace_s = cancel_grace_s

        self.phase = Phase.BOOTING
        self.agent_session_id: str | None = None
        #: open 时确定的会话默认时限（server 给的 defaults 覆盖兜底值）
        self.limits: TurnLimits = fallback_limits
        self.turn: Turn | None = None
        self.ledger = TurnLedger(ledger_size)
        self.permissions = PermissionBroker(outbox, clock, max_pending=max_pending_asks)
        #: 会话结束（不论谁发起）时 set；上游层据此以 4410 关闭连接
        self.ended = asyncio.Event()

        self._booted = asyncio.Event()
        self._replaying = False
        #: open 时 server 给的 MCP 配置，恢复会话时原样再用一次
        self._mcp_servers: list[JsonObject] = []
        #: agent 进程是否仍可信（取消不响应之后不再向它发 session/close）
        self._agent_healthy = True
        self._tasks: set[asyncio.Task[Any]] = set()
        #: 上游断开时计时；窗口内没有重连上，就以 upstream_lost 取消进行中的一轮（§7.2）。
        #: 默认不设窗口：断线只是通道断了，这一轮照常进行，消息缓冲到 server 回来 attach
        self._upstream_connected = False
        self._reconnect = Timer(clock, self._on_reconnect_window, name="reconnect-window")
        #: server 要的权限模式（最近一次 turn.start 给的）。agent 每次新建 / 恢复会话都会
        #: 把模式重置，所以要记住它，恢复会话后重新应用
        self._desired_mode: str | None = None
        #: agent 当前实际生效的模式与可选列表。None / () = agent 不支持模式
        self._mode_current: str | None = None
        self._mode_available: tuple[str, ...] = ()
        #: turn.start 要先 await 设置模式：串行化，免得两个 turn.start 同时通过「没有进行中的
        #: 一轮」这道检查
        self._start_lock = asyncio.Lock()
        agent.bind(self)

    # ═══════════════════════════════════ 启动 ═══════════════════════════════════

    async def boot(self) -> None:
        """预热：拉起 agent 并完成 initialize（§5.5）。BOOTING → IDLE。"""
        assert self.phase is Phase.BOOTING
        try:
            await self._agent.boot()
        finally:
            self._booted.set()
        if self.phase is Phase.BOOTING:
            self.phase = Phase.IDLE

    # ═══════════════════════════════════ 上游指令 ═══════════════════════════════════

    async def open(self, params: OpenParams) -> OpenResult:
        """IDLE → OPENING → READY。重放的历史在响应之前以 agent.update(replay) 送出。"""
        if self.phase is Phase.BOOTING:
            await self._booted.wait()  # 预热还没结束：等它（§6.5）
            if self.phase is Phase.BOOTING:
                raise AgentUnavailable("boot_failed")
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            raise SessionNotOpen("ended")
        if self.phase is not Phase.IDLE:
            raise SessionAlreadyOpen(self._bridge.instance, self.agent_session_id)

        self.phase = Phase.OPENING
        self.limits = params.defaults.over(self._fallback)
        self._mcp_servers = params.mcp_servers
        try:
            session_id, resumed, replayed = await self._open_agent_session(params.resume)
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

        negotiated = self._agent.negotiated
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
            mode = await self._apply_mode(self.agent_session_id, params.mode)
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
            client=self._agent.client,
            outbox=self._outbox,
            clock=self._clock,
            owner=self,
            cancel_grace_s=self._cancel_grace_s,
            mode=mode,
        )
        self.turn = turn
        self.phase = Phase.IN_TURN
        if mode is not None and self._too_permissive(mode):
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

    def attach(self, params: AttachParams) -> AttachResult:
        """重连后的第一条请求（§7.3）。响应之后由 Outbox 按 seq 顺序补发 resendFrom 起的消息。

        ★ server 的判断依据是 state 与 turn，不是补发的消息：会话此刻处于什么状态以这里为准。
        """
        if params.bridge_instance != self._bridge.instance:
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
        resend_from, gaps = self._outbox.resend_plan(params.last_seq)
        return AttachResult(
            state=state,
            turn=TurnRef(turn_id=turn.turn_id, state=turn.state) if turn and turn.state else None,
            resend_from=resend_from,
            gaps=[SeqRange(first=a, last=b) for a, b in gaps],
        )

    def upstream_changed(self, connected: bool) -> None:
        """上游连接建立 / 断开（由 UpstreamServer 调用）。断开不结束这一轮（§7.2）。"""
        self._upstream_connected = connected
        if connected:
            self._reconnect.stop()
        elif self.turn is not None and not self.turn.ended:
            window = self.turn.limits.reconnect_window_s
            if window is not None:
                self._reconnect.start(window)

    def _on_reconnect_window(self) -> None:
        if not self._upstream_connected and self.turn is not None:
            logger.warning("上游在重连窗口内没有回来，取消进行中的一轮")
            self.turn.begin_cancel(CancelCause.UPSTREAM_LOST)

    async def close(self) -> None:
        """若有一轮在进行，先按取消流程结束它；再关闭 agent 会话、终止 agent。幂等。"""
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            await self.ended.wait()
            return
        self.phase = Phase.CLOSING
        turn = self.turn
        if turn is not None:
            turn.begin_cancel(CancelCause.SESSION_CLOSING)
            await turn.done.wait()
        if self.agent_session_id is not None and self._agent_healthy:
            await self._close_agent_session(self.agent_session_id)
        await self._agent.shutdown()
        self.phase = Phase.ENDED
        self.ended.set()

    def on_permission_answer(self, ask_id: int, result: Any = None, error: Any = None) -> None:
        """server 对 permission.ask 的响应（由上游连接按 id 转交）。"""
        self.permissions.on_answer(ask_id, result, error)

    # ═══════════════════════════════════ 来自 agent ═══════════════════════════════════

    async def on_agent_update(self, raw: dict[str, Any]) -> None:
        """定 origin → Outbox；属于当前一轮的，再交给 Turn 读取进展信号。

        ★ 先等 Outbox 可写：缓冲满时这里停下，AcpClient 的读循环随之停下，压力传回 agent（§6.6）。
        """
        self._observe_mode(raw)
        await self._outbox.wait_writable()
        if self.phase is Phase.ENDED:
            return
        turn = self.turn
        if self._replaying:
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

    def on_agent_lost(self, cause: FailCause) -> None:
        """agent 进程退出。关闭过程中的退出是预期内的。"""
        if self.phase in (Phase.CLOSING, Phase.ENDED):
            return
        self._agent_healthy = False
        if self.turn is not None:
            self.turn.fail(cause)  # → turn_ended → _agent_broken
        else:
            self._agent_broken(cause.value)

    # ═══════════════════════════════════ TurnOwner ═══════════════════════════════════

    def settle_permissions(self, turn: Turn) -> None:
        self.permissions.settle_all(turn)

    def turn_ended(self, turn: Turn) -> None:
        assert turn.outcome is not None and turn.stats is not None
        self.ledger.record(turn.turn_id, TurnRecord(turn.outcome, turn.stats))
        self._reconnect.stop()
        if self.turn is turn:
            self.turn = None
        if self.phase is Phase.IN_TURN:
            self.phase = Phase.READY
        outcome = turn.outcome
        if isinstance(outcome, Failed) and outcome.cause in _AGENT_BROKEN:
            self._agent_healthy = False
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

    async def _open_agent_session(self, resume: ResumeSpec | None) -> tuple[str, bool, Any]:
        """按能力位选方法；恢复失败不是错误 —— 新建会话并如实报告 resumed=False（§5.6）。"""
        client = self._agent.client
        mcp = self._mcp_servers
        timeout = self._open_timeout_s
        plan = plan_open(self._agent.negotiated.caps, resume.replay if resume else None)

        if resume is not None and plan.method is not OpenMethod.NEW:
            try:
                if plan.method is OpenMethod.LOAD:
                    self._replaying = True
                    await client.load_session(
                        resume.agent_session_id, self._workspace, mcp, timeout=timeout
                    )
                else:
                    await client.resume_session(
                        resume.agent_session_id, self._workspace, mcp, timeout=timeout
                    )
                self._absorb_modes(resume.agent_session_id)
                return resume.agent_session_id, True, plan.replayed
            except AgentRequestFailed as exc:
                logger.warning("恢复会话 %s 失败，改为新建：%s", resume.agent_session_id, exc)
            finally:
                self._replaying = False

        session_id = await client.new_session(self._workspace, mcp, timeout=timeout)
        self._absorb_modes(session_id)
        return session_id, False, "none"

    # ------------------------------------------------------------------ 权限模式

    def _absorb_modes(self, session_id: str) -> None:
        """会话刚打开：以 agent 报告的模式为准（它在新建 / 恢复时会被重置）。"""
        modes = self._agent.client.modes_of(session_id)
        self._mode_current = modes.current if modes else None
        self._mode_available = modes.available if modes else ()

    def _observe_mode(self, raw: dict[str, Any]) -> None:
        """agent 自己换了模式（降级、退出 plan 等）会发 update：跟上实际生效的那个。"""
        update = raw.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update" and isinstance(update.get("currentModeId"), str):
            self._mode_current = update["currentModeId"]
        elif kind == "config_option_update":
            for option in update.get("configOptions") or []:
                if (
                    isinstance(option, dict)
                    and option.get("id") == "mode"
                    and isinstance(option.get("currentValue"), str)
                ):
                    self._mode_current = option["currentValue"]

    async def _apply_mode(self, session_id: str, requested: str) -> ModeState:
        """把 agent 的模式设成 requested（已经是就不动）。失败不抛：如实报告实际生效的模式。"""
        self._desired_mode = requested
        if not self._mode_available:
            return ModeState(requested=requested, effective=None, degraded=True)
        before = self._mode_current
        if before != requested:
            if requested not in self._mode_available:
                logger.warning(
                    "agent 不支持权限模式 %s（可选：%s）", requested, self._mode_available
                )
            else:
                try:
                    await self._agent.client.set_mode(
                        session_id, requested, timeout=self._request_timeout_s
                    )
                except (AgentRequestFailed, AcpRequestTimeout, ChannelClosed) as exc:
                    logger.warning("设置权限模式 %s 失败：%s", requested, exc)
                else:
                    # agent 自己降级时会先发 update（已由 _observe_mode 记下）；没发就是照办了
                    if self._mode_current == before:
                        self._mode_current = requested
        return ModeState(
            requested=requested,
            effective=self._mode_current,
            available=list(self._mode_available),
            degraded=self._mode_current != requested,
        )

    @staticmethod
    def _too_permissive(mode: ModeState) -> bool:
        """要求的设不上、而实际的比要求的更宽松 → 不能跑。agent 不支持模式时无从判断，放行。"""
        if not mode.degraded or mode.requested is None or mode.effective is None:
            return False
        return _rank(mode.effective) > _rank(mode.requested)

    async def _close_agent_session(self, session_id: str) -> None:
        if not self._agent.negotiated.caps.close:
            return
        try:
            await self._agent.client.close_session(session_id, timeout=self._request_timeout_s)
        except (AgentRequestFailed, AcpRequestTimeout, ChannelClosed) as exc:
            # 会话本来就要结束了，随后直接终止进程（§6.5）
            logger.warning("关闭 agent 会话失败：%s", exc)

    def _agent_broken(self, cause: str) -> None:
        """agent 已不可用：重启它。已在重启中、正在关闭时不重复处理。"""
        self._agent_healthy = False
        match self.phase:
            case Phase.IDLE | Phase.OPENING:
                self.phase = Phase.BOOTING
                self._booted.clear()
                self._spawn(self._reboot(cause))
            case Phase.READY | Phase.IN_TURN:
                self.phase = Phase.RECOVERING
                restarts = self._agent.restarts + 1
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
            await self._agent.restart(cause)
        except AgentUnavailable as exc:
            if self.phase is Phase.BOOTING:
                self._end(exc.cause, ErrorInfo(cause=exc.cause))
            return
        finally:
            self._booted.set()
        if self.phase is Phase.BOOTING:
            self._agent_healthy = True
            self.phase = Phase.IDLE

    async def _recover(self, cause: str) -> None:
        """RECOVERING：重启 agent，恢复同一个会话，不重放（§5.6）。"""
        previous = self.agent_session_id
        assert previous is not None
        while True:
            try:
                await self._agent.restart(cause)
            except AgentUnavailable as exc:
                if self.phase is Phase.RECOVERING:
                    self._end(exc.cause, ErrorInfo(cause=exc.cause))
                return
            if self.phase is not Phase.RECOVERING:
                return
            try:
                session_id, resumed, _ = await self._open_agent_session(
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
        mode: ModeState | None = None
        if self._desired_mode is not None:
            mode = await self._apply_mode(session_id, self._desired_mode)
            if self.phase is not Phase.RECOVERING:
                return
        self._agent_healthy = True
        self.phase = Phase.READY
        restart = AgentRestart(
            restarts=self._agent.restarts, resumed=resumed, agent_session_id=session_id, mode=mode
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
