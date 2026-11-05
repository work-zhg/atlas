"""CLI 运行时 · 一轮的编排（c4-model/agent/cli-runtime/code-cli-runtime-turn.html）。

对 Run 执行器来说它和原生运行时是同一个接口（AgentRuntime）：给它一段，拿回一串平台事件，最后
一条是终止事件。顺序固定：

    检查 CLI 配置 → 解析本轮能力（CliCapabilityAssembly）→ 确保 Pod → 接上会话（SessionLink）
      → 开一轮（turnId = run id，重复提交安全）
      → 收通知：内容交给翻译（AcpV1Translator）、权限请求交给权限转发（PermissionDesk）、
        用户取消转成 turn.cancel
      → 按结果产出终止事件（TurnOutcome）

轮次的计时、取消流程、CLI 崩溃重启都在 Pod 里的 bridge，这里只提要求、解读结果。和原生运行时
一样不落库、不发布、不管锁；唯一的写库是会话的接续信息（cli_session，上游连接领域管）。

★ 一轮没有时间上限，审批没有等待上限，断线重连没有窗口：CLI 在干活就不中断，审批只由人决定，
  断线只是通道断了（这一轮在 Pod 里照常进行）。一轮只在这几种情况下结束：CLI 自己结束或报错、
  CLI 进程死了、静默检测判定卡死、用户取消、bridge 重启或 Pod 没了。
★ 换实例接管：接手的实例从 cli_session 读出 bridge 实例和确认点 attach 回去、从确认点之后补发 ——
  那一轮在 Pod 里一直在跑，接管只是把事件流接回来，工具不会重复执行。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from atlas_host import methods
from atlas_jsonrpc import ErrorCode, RpcFault

from ..approval import ApprovalService, DecisionChannel, OnlineApprovals
from ..domain.events import EventFactory, EventType, TraceEvent
from ..domain.skill_events import SkillLoadTracker
from ..errors import CapabilityUnavailable
from .capabilities import CliCapabilities, CliCapabilityAssembly
from .link import CliSession, CliSessionStore, DbCliSessions, SessionLink
from .modes import ModeMapping
from .outcome import TurnAbort, failed, note_failed_tool, terminal_events
from .permission import ApprovalPort, PermissionDesk
from .translate.acp_v1 import AcpV1Translator

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings
    from ..executor.segment_data import Segment
    from ..sandbox.session_pod import PodEndpoint, PodProvider
    from ..stream.relay import EventRelay

__all__ = ["CliRuntime"]

logger = logging.getLogger(__name__)

#: 取消信号的轮询间隔（等的是远端的一轮，要主动问）
_POLL_S = 0.5


class ApprovalFactory(Protocol):
    def __call__(self, run_id: UUID, redis: aioredis.Redis) -> ApprovalPort: ...


def _awaiting_sync(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: UUID
) -> Callable[[], Awaitable[None]]:
    """在线等待期间 run 标 awaiting_user（状态仍是 running）。

    ★ 每次都按「这个 run 还有没有待决的审批」重算，不跟着单次请求翻转：同一轮里可能同时
      挂着两个请求，答完一个时另一个还在等。
    """
    from ..repositories.run_record import RunRecordRepository

    async def sync() -> None:
        async with sessionmaker() as session:
            waiting = bool(await ApprovalService(session).pending(run_id))
            await RunRecordRepository(session).set_awaiting_user(run_id, waiting=waiting)
            await session.commit()

    return sync


class CliRuntime:
    """进程级构造一次；每轮变化的东西全在 run_turn 的参数里。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        pods: PodProvider,
        *,
        approvals: ApprovalFactory | None = None,
        sessions: CliSessionStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._pods = pods
        self._capabilities = CliCapabilityAssembly(settings)
        self._approvals: ApprovalFactory = approvals or (
            lambda run_id, redis: OnlineApprovals(sessionmaker, DecisionChannel(redis), run_id)
        )
        self._sessions: CliSessionStore = sessions or DbCliSessions(sessionmaker)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_turn(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        transcript: Any = None,
    ) -> AsyncIterator[TraceEvent]:
        events = EventFactory(
            run_id, self._clock, start_seq=segment.start_seq, base_depth=segment.base_depth
        )
        spec = segment.spec
        cli = spec.cli
        yield events.make(
            EventType.RUN_STARTED,
            {
                "agent_slug": spec.slug,
                "agent_name": spec.name,
                "model": cli.cli_type if cli else "",
                "effort": None,
                "thinking": "none",
                "tools": sorted(spec.tool_names),
                # 审计：这一轮按什么权限模式要求 CLI（实际生效的见 agent.mode 事件）
                **(
                    {"permission_mode": ModeMapping.platform_mode(cli, segment.approval_mode)}
                    if cli
                    else {}
                ),
            },
        )
        if cli is None:
            yield events.make(*failed("invalid_spec", "acp agent 缺少 cli 配置"))
            return
        try:
            caps = await self._capabilities.resolve(segment, redis)
        except CapabilityUnavailable as exc:
            yield events.make(*failed(CapabilityUnavailable.kind, str(exc)))
            return
        for kind, data in caps.notices:
            yield events.make(kind, data)
        try:
            endpoint = await self._pods.ensure(
                segment.thread, cli, model=caps.model, credentials=caps.credentials
            )
        except Exception as exc:
            yield events.make(*failed("pod_unavailable", f"会话 Pod 不可用：{exc}"))
            return

        turn_id = str(run_id)
        #: 权限转发产出的事件（approval.required、tool.failed）和 bridge 的通知走同一个队列，
        #: 按到达顺序处理
        queue: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue()
        desk = PermissionDesk(
            self._approvals(run_id, redis),
            emit=lambda kind, data: queue.put_nowait(("event", kind, data)),
            run_id=run_id,
            awaiting=(
                _awaiting_sync(self._sessionmaker, run_id)
                if self._sessionmaker is not None
                else None
            ),
        )

        def on_note(method: str, params: Any) -> None:
            if method == methods.PERMISSION_WITHDRAW and isinstance(params, dict):
                desk.withdraw(params.get("askId"), str(params.get("reason", "")))
            queue.put_nowait(("note", method, params))

        async def on_request(method: str, params: Any) -> Any:
            if method == methods.PERMISSION_ASK and isinstance(params, dict):
                if params.get("turnId") != turn_id:
                    return {"reject": True}  # 不属于本轮（上一轮遗留的询问）：没有人会看到它
                return await desk.ask(params)
            raise RpcFault(ErrorCode.METHOD_NOT_FOUND, f"不支持的方法：{method}")

        link = SessionLink(
            endpoint,
            CliSession(
                segment.thread_id,
                agent_session_id=segment.cli_session_id,
                bridge_instance=segment.cli_bridge_instance,
                acked_seq=segment.cli_acked_seq,
            ),
            self._sessions,
            on_note=on_note,
            on_request=on_request,
            connect_timeout_s=self._settings.cli_ws_connect_timeout_s,
        )
        try:
            async for event in self._drive(link, queue, segment, caps, events, relay, run_id):
                yield event
        finally:
            await link.close()

    # ------------------------------------------------------------------ 一轮

    async def _drive(
        self,
        link: SessionLink,
        queue: asyncio.Queue[tuple[Any, ...]],
        segment: Segment,
        caps: CliCapabilities,
        events: EventFactory,
        relay: EventRelay,
        run_id: UUID,
    ) -> AsyncIterator[TraceEvent]:
        assert segment.spec.cli is not None
        cli = segment.spec.cli
        turn_id = str(run_id)
        #: 只用于解读结果的措辞：一轮不设截止，bridge 不会因时间结束它
        timeout = float(segment.spec.limits.timeout_s)
        try:
            for kind, data in await link.join(
                mcp_servers=caps.mcp_servers,
                limits=caps.limits,
                takeover=segment.takeover is not None,
            ):
                yield events.make(kind, data)
            await link.save_progress()
            await link.start_turn(
                turn_id,
                _text_of(segment.input_content),
                ModeMapping.to_cli(cli, segment.approval_mode),
            )
        except TurnAbort as abort:
            yield events.make(*abort.intent)
            return

        translator = AcpV1Translator()
        # CLI 读了 /skills/<slug>/SKILL.md → skill.loaded（与 native 同一份规则）
        skills = SkillLoadTracker(segment.spec.skills)
        failed_tools: dict[str, str] = {}
        cancel_sent = False

        async def reach() -> PodEndpoint:
            # 断线重连时 Pod 可能被重建：下发同一份模型接入
            return await self._pods.ensure(
                segment.thread, cli, model=caps.model, credentials=caps.credentials
            )

        async def cancelled() -> bool:
            return await relay.is_cancelled(run_id)

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), _POLL_S)
            except TimeoutError:
                item = None

            if item is not None and item[0] == "event":
                event = events.make(item[1], item[2])
                if event.type is EventType.TOOL_FAILED:
                    note_failed_tool(failed_tools, event.data)
                yield event
                continue
            if item is not None:
                _, method, params = item
                if not link.inbox.accept(params):
                    continue
                intents, outcome = await self._on_note(method, params, link, turn_id, translator)
                for kind, data in intents:
                    event = events.make(kind, data)
                    if event.type is EventType.TOOL_FAILED:
                        note_failed_tool(failed_tools, event.data)
                    yield event
                    for extra_kind, extra in skills.observe(event.type, event.data):
                        yield events.make(extra_kind, extra)
                if outcome is not None:
                    for kind, data in terminal_events(outcome, translator, failed_tools, timeout):
                        yield events.make(kind, data)
                    await link.acknowledge(force=True)
                    return
                await link.acknowledge()
                continue

            await link.acknowledge()
            if link.closed and queue.empty():
                # ★ 断线不等于这一轮结束：bridge 那边照常进行。重连并 attach，补全事件流
                try:
                    reattached = await link.reconnect(reach, cancelled)
                except TurnAbort as abort:
                    yield events.make(*abort.intent)
                    return
                if not reattached:
                    # 断线期间用户取消了：以取消收尾。bridge 上那一轮由下一轮的 turn.start
                    # （TURN_BUSY → 先取消它）收掉
                    stopped = {"kind": "cancelled", "cause": "requested"}
                    for kind, data in terminal_events(stopped, translator, failed_tools, timeout):
                        yield events.make(kind, data)
                    return
                continue
            if not cancel_sent and await relay.is_cancelled(run_id):
                cancel_sent = True
                await link.cancel_turn(turn_id)

    async def _on_note(
        self,
        method: str,
        params: dict[str, Any],
        link: SessionLink,
        turn_id: str,
        translator: AcpV1Translator,
    ) -> tuple[list[tuple[EventType, dict[str, Any]]], dict[str, Any] | None]:
        """一条 bridge 的通知 → (事件意图, 本轮的 outcome 或 None)。"""
        if method == methods.AGENT_UPDATE:
            # 只翻译本轮的；replay（server 没要历史）与 stray 不进事件流
            if params.get("origin") == "turn" and params.get("turnId") == turn_id:
                update = params.get("update")
                return (translator.update(update) if isinstance(update, dict) else []), None
            return [], None
        if method == methods.TURN_STATE:
            if params.get("turnId") != turn_id:
                return [], None
            if params.get("state") == "ended":
                return [], params.get("outcome") or {}
            mode = params.get("mode")
            if params.get("state") == "running" and isinstance(mode, dict):
                return [(EventType.AGENT_MODE, ModeMapping.from_cli(mode))], None
            return [], None
        if method == methods.SESSION_STATE:
            agent = params.get("agent") or {}
            if params.get("state") == "ready":
                new_id = agent.get("agentSessionId")
                if new_id:
                    await link.remember(str(new_id))
                if agent.get("resumed") is False:
                    return [
                        (
                            EventType.SESSION_LOST,
                            {"reason": "CLI 进程重启后未能恢复会话 —— 之前的上下文不在了"},
                        )
                    ], None
            return [], None
        if method == methods.SESSION_ENDED:
            cause = str(params.get("cause", ""))
            return [], {"kind": "failed", "cause": "session_ended", "detail": cause}
        return [], None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return str(content)
