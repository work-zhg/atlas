"""HostRuntime：AgentRuntime 的新 bridge（atlas.host.v1）实现（代码设计 §11）。

对平台其余部分而言，它和 NativeRuntime 是同一个接口（AgentRuntime）：给它一轮的输入，
拿回一串平台事件，最后一条是终止事件。

一轮的编排：
    确保 Pod → 连上 bridge → session.open（恢复 thread 记录的 agentSessionId）
      → turn.start（turnId = run id，重复提交安全）
      → agent.update 经翻译变成 TraceEvent；permission.ask 接到平台审批
      → turn.state(ended) 的 outcome → 终止事件

与 bridge 的分工（Bridge 设计 §3 D1）：轮次的计时、取消流程、ACP 的取消义务、
agent 崩溃后的重启与会话恢复都在 bridge 里；这里只提出要求（时限、取消）并解读结果。

★ 与 NativeRuntime 一样：不落库、不发布、不管锁。唯一的写库是 external_session_id ——
  它是会话身份，下一轮的恢复全靠它。

连接：一轮一条。认识这个 bridge（进程内记住了实例 id 与 lastSeq）就 session.attach，
否则 session.open；一轮进行中断线则退避重连并 attach，补全事件流（§7.3 · §7.5）。
bridge 重启过（实例 id 变了）→ 这一轮以 bridge_lost 失败，下一轮 session.open 恢复（§7.6）。

★ 一轮没有时间上限，审批没有等待上限，断线重连没有窗口：CLI 在干活就不中断，
  审批只由人决定，断线只是通道断了（这一轮在 Pod 里照常进行）。一轮只在这几种情况下结束：
  CLI 自己结束或报错、CLI 进程死了、静默检测判定卡死、用户取消、bridge 重启或 Pod 没了。
尚未实现：lastSeq 与事件同事务持久化、多实例租约 —— 目前由执行器的会话串行锁保证
同一会话只有一个 run（也就只有一条连接），server 重启时进行中的 run 由启动回收判为中断。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

from atlas_host import CloseCode, HostErrorCode, methods
from atlas_jsonrpc import ChannelClosed, ErrorCode, RpcFault
from sqlalchemy import update as sql_update

from ..configplane.mcp import make_mcp_catalog, mcp_reviews
from ..db.models import Thread
from ..domain.events import EventFactory, EventType, TraceEvent
from ..domain.mcp_naming import MCP_PREFIX
from ..domain.skill_events import SkillLoadTracker
from ..domain.spec import ACP_MODE_ID, PLATFORM_MODE
from ..errors import CapabilityUnavailable
from ..providers.mcp.acp import acp_mcp_servers
from ..repositories.approval import ApprovalRepository
from ..services.approval import RedisApprovalGate
from .approvals import ApprovalPort, PermissionDesk
from .client import HostClient, HostUnreachable
from .translate.acp_v1 import (
    AcpV1Translator,
    model_of,
    translate_crash,
    translate_stop,
    usage_of,
)

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings
    from ..executor.assembly import PreparedRun
    from ..providers.pods import PodProvider
    from ..stream.relay import EventRelay

__all__ = ["HostRuntime", "note_failed_tool", "silent_failure_message"]

logger = logging.getLogger(__name__)

#: 取消信号的轮询间隔（等的是远端的一轮，要主动问）
_POLL_S = 0.5
#: session.open（agent 启动 + 恢复）与普通请求的超时
_OPEN_TIMEOUT_S = 150.0
_REQUEST_TIMEOUT_S = 30.0
#: bridge 正在恢复 / 上一轮还没结束时，turn.start 的重试窗口
_START_RETRY_S = 60.0
#: session.ack 的节奏：每 1 s 或每 200 条，以先到者为准（§7.3）
_ACK_EVERY = 200
_ACK_INTERVAL_S = 1.0
#: 断线重连的退避上限。重连本身不设窗口
_RECONNECT_MAX_DELAY_S = 30.0
#: 没有上报用量时的 usage.updated（token 照样要记账，只是记为 0）
_ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cache_read": 0,
    "cache_creation": 0,
    "thinking_tokens": 0,
}


class SessionStore(Protocol):
    async def remember(self, thread_id: UUID, agent_session_id: str) -> None: ...


class ApprovalFactory(Protocol):
    def __call__(self, run_id: UUID, redis: aioredis.Redis) -> ApprovalPort: ...


# ═══════════════════════════════════ 默认的库实现 ═══════════════════════════════════


class _DbSessions:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def remember(self, thread_id: UUID, agent_session_id: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                sql_update(Thread)
                .where(Thread.id == thread_id)
                .values(external_session_id=agent_session_id)
            )
            await session.commit()


class _DbApprovals:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], redis: aioredis.Redis, run_id: UUID
    ) -> None:
        self._sessionmaker = sessionmaker
        self._gate = RedisApprovalGate(sessionmaker, redis, run_id)

    async def check(self, *, approval_id: str, tool_name: str, args: dict[str, Any]) -> str:
        return await self._gate.check(approval_id=approval_id, tool_name=tool_name, args=args)

    async def expire(self, approval_id: str) -> None:
        async with self._sessionmaker() as session:
            await ApprovalRepository(session).expire(UUID(approval_id))
            await session.commit()


# ═══════════════════════════════════ 运行时 ═══════════════════════════════════


class HostRuntime:
    """进程级构造一次；每轮变化的东西全在 run_turn 的参数里。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        pods: PodProvider,
        *,
        approvals: ApprovalFactory | None = None,
        sessions: SessionStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._pods = pods
        self._approvals: ApprovalFactory = approvals or (
            lambda run_id, redis: _DbApprovals(sessionmaker, redis, run_id)
        )
        self._sessions: SessionStore = sessions or _DbSessions(sessionmaker)
        self._clock = clock or (lambda: datetime.now(UTC))
        #: thread → 上次见到的 bridge（实例 id、已处理到的 seq）。进程内记忆：
        #: 有它就用 session.attach 接上会话；server 重启后丢失，退回 session.open（§7.6）
        self._known: dict[UUID, _Known] = {}

    async def run_turn(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        transcript: Any = None,
    ) -> AsyncIterator[TraceEvent]:
        events = EventFactory(
            run_id, self._clock, start_seq=prepared.start_seq, base_depth=prepared.base_depth
        )
        spec = prepared.spec
        yield events.make(
            EventType.RUN_STARTED,
            {
                "agent_slug": spec.slug,
                "agent_name": spec.name,
                "model": spec.cli.cli_type if spec.cli else "",
                "effort": None,
                "thinking": "none",
                "tools": sorted(spec.tool_names),
                # 审计：这一轮按什么权限模式要求 CLI（实际生效的见 agent.mode 事件）
                **({"permission_mode": spec.cli.permission_mode} if spec.cli else {}),
            },
        )
        if spec.cli is None:
            yield events.make(*_failed("invalid_spec", "acp agent 缺少 cli 配置"))
            return
        # ★ MCP 在建会话（session.open）时一次性下发，之后整个会话沿用（§10）
        try:
            mcp_servers, notices = await self._mcp_servers(prepared, redis)
        except CapabilityUnavailable as exc:
            yield events.make(*_failed(CapabilityUnavailable.kind, str(exc)))
            return
        for kind, data in notices:
            yield events.make(kind, data)
        try:
            endpoint = await self._pods.ensure(prepared.thread, spec.cli)
        except Exception as exc:
            yield events.make(*_failed("pod_unavailable", f"会话 Pod 不可用：{exc}"))
            return

        turn = _Turn(str(run_id))
        desk = PermissionDesk(
            self._approvals(run_id, redis),
            emit=lambda kind, data: turn.queue.put_nowait(("event", kind, data)),
        )

        async def on_notification(method: str, params: Any) -> None:
            if method == methods.PERMISSION_WITHDRAW and isinstance(params, dict):
                desk.withdraw(params.get("askId"), str(params.get("reason", "")))
            turn.queue.put_nowait(("note", method, params))

        async def on_request(method: str, params: Any) -> Any:
            if method == methods.PERMISSION_ASK and isinstance(params, dict):
                if params.get("turnId") != turn.turn_id:
                    return {"reject": True}  # 不属于本轮（上一轮遗留的询问）：没有人会看到它
                return await desk.ask(params)
            raise RpcFault(ErrorCode.METHOD_NOT_FOUND, f"不支持的方法：{method}")

        link = _Link(
            _host_url(endpoint.url),
            token=endpoint.token,
            session_id=str(prepared.thread_id),
            on_notification=on_notification,
            on_request=on_request,
            connect_timeout_s=self._settings.acp_ws_connect_timeout_s,
        )
        try:
            async for event in self._drive(
                link, prepared, turn, events, relay, run_id, mcp_servers
            ):
                yield event
        finally:
            await link.close()

    # ------------------------------------------------------------------ 一轮

    async def _drive(
        self,
        link: _Link,
        prepared: PreparedRun,
        turn: _Turn,
        events: EventFactory,
        relay: EventRelay,
        run_id: UUID,
        mcp_servers: list[dict[str, Any]],
    ) -> AsyncIterator[TraceEvent]:
        #: 只用于解读结果的措辞：一轮不设截止，bridge 不会因时间结束它
        timeout = float(prepared.spec.limits.timeout_s)
        try:
            for kind, data in await self._join(link, prepared, mcp_servers):
                yield events.make(kind, data)
            # 本轮从已处理到的位置接着收：断线重连时 attach 从这里补发
            turn.last_seq = self._known[prepared.thread_id].last_seq
            await self._start(link.client, prepared, turn)
        except _Abort as abort:
            yield events.make(*abort.intent)
            return

        translator = AcpV1Translator()
        # CLI 读了 /skills/<slug>/SKILL.md → skill.loaded（与 native 同一份规则）
        skills = SkillLoadTracker(prepared.spec.skills)
        failed_tools: dict[str, str] = {}
        cancel_sent = False
        known = self._known[prepared.thread_id]

        while True:
            try:
                item = await asyncio.wait_for(turn.queue.get(), _POLL_S)
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
                if not turn.accept(params):
                    continue
                intents, outcome = await self._on_note(method, params, prepared, turn, translator)
                for kind, data in intents:
                    event = events.make(kind, data)
                    if event.type is EventType.TOOL_FAILED:
                        note_failed_tool(failed_tools, event.data)
                    yield event
                    for extra_kind, extra in skills.observe(event.type, event.data):
                        yield events.make(extra_kind, extra)
                # 走到这里，这条消息翻出的事件已经交出去了：它算「处理完成」
                known.last_seq = turn.last_seq
                if outcome is not None:
                    for kind, data in _finish(outcome, translator, failed_tools, timeout):
                        yield events.make(kind, data)
                    await turn.ack(link.client, force=True)
                    return
                await turn.ack(link.client)
                continue

            await turn.ack(link.client)
            if link.client.closed and turn.queue.empty():
                # ★ 断线不等于这一轮结束：bridge 那边照常进行（§7.2）。重连并 attach，补全事件流
                try:
                    reattached = await self._reconnect(link, prepared, turn, relay, run_id)
                except _Abort as abort:
                    yield events.make(*abort.intent)
                    return
                if not reattached:
                    # 断线期间用户取消了：以取消收尾。bridge 上那一轮由下一轮的 turn.start
                    # （TURN_BUSY → 先取消它）收掉
                    cancelled = {"kind": "cancelled", "cause": "requested"}
                    for kind, data in _finish(cancelled, translator, failed_tools, timeout):
                        yield events.make(kind, data)
                    return
                continue
            if not cancel_sent and await relay.is_cancelled(run_id):
                cancel_sent = True
                with contextlib.suppress(RpcFault, ChannelClosed, TimeoutError):
                    await link.client.request(
                        methods.TURN_CANCEL, {"turnId": turn.turn_id}, timeout=_REQUEST_TIMEOUT_S
                    )

    # ------------------------------------------------------------------ MCP

    async def _mcp_servers(
        self, prepared: PreparedRun, redis: aioredis.Redis
    ) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]]]:
        spec = prepared.spec
        if not any(name.startswith(MCP_PREFIX) for name in spec.tool_names):
            return [], []
        catalog = await make_mcp_catalog(self._settings, redis)
        return await acp_mcp_servers(
            spec.tool_names,
            catalog,
            user_id=prepared.thread.created_by,
            digests=spec.mcp_tool_digests,
            drift_policy=spec.mcp_drift_policy,
            reviews=await mcp_reviews(self._settings, catalog.servers()),
            timeout_s=self._settings.mcp_call_timeout_s,
        )

    # ------------------------------------------------------------------ 接上会话

    async def _join(
        self, link: _Link, prepared: PreparedRun, mcp_servers: list[dict[str, Any]]
    ) -> list[tuple[EventType, dict[str, Any]]]:
        """连上 bridge 并接上会话：认识这个 bridge 就 attach，否则 open。"""
        try:
            await link.connect()
        except HostUnreachable as exc:
            raise _Abort(_failed("runtime_unreachable", str(exc))) from exc
        known = self._known.get(prepared.thread_id)
        if known is not None:
            try:
                await self._attach(link, known.instance, known.last_seq)
                return []
            except RpcFault as fault:
                if fault.code != HostErrorCode.SESSION_NOT_OPEN:
                    raise _Abort(_fault_intent(fault)) from fault
                # bridge 重启过（或还没打开过会话）：它的进程内状态已不在，重新 open 恢复
                logger.info("bridge 不认识这个会话了（%s），改为 session.open", fault.data)
                self._known.pop(prepared.thread_id, None)
        return await self._open(link, prepared, mcp_servers)

    async def _attach(self, link: _Link, instance: str, last_seq: int) -> dict[str, Any]:
        try:
            result = await link.client.request(
                methods.SESSION_ATTACH,
                {"bridgeInstance": instance, "lastSeq": last_seq},
                timeout=_REQUEST_TIMEOUT_S,
            )
        except (ChannelClosed, TimeoutError) as exc:
            raise HostUnreachable(f"session.attach 失败：{exc!r}") from exc
        if result.get("gaps"):
            # 补发不完整：缺的只是一段展示内容，会话与轮次的状态仍然完整（§7.4）
            logger.warning("bridge 的补发缓冲溢出过，缺失区间 %s", result["gaps"])
        return result  # type: ignore[no-any-return]

    async def _open(
        self, link: _Link, prepared: PreparedRun, mcp_servers: list[dict[str, Any]]
    ) -> list[tuple[EventType, dict[str, Any]]]:
        existing = prepared.thread.external_session_id
        params: dict[str, Any] = {
            "mcpServers": mcp_servers,
            # ★ 只下发静默检测的两个阈值（唯一的「卡死」判据）。截止、审批等待、
            #   重连窗口不下发 = 不限：CLI 在干活就不中断，审批只由人决定
            "defaults": {
                "idleS": self._settings.acp_prompt_idle_timeout_s,
                "toolIdleS": self._settings.acp_tool_idle_timeout_s,
            },
        }
        if existing:
            params["resume"] = {"agentSessionId": existing, "replay": "none"}
        try:
            opened = await link.client.request(
                methods.SESSION_OPEN, params, timeout=_OPEN_TIMEOUT_S
            )
        except RpcFault as fault:
            data = fault.data if isinstance(fault.data, dict) else {}
            if fault.code == HostErrorCode.SESSION_ALREADY_OPEN and data.get("bridgeInstance"):
                # Pod 上的会话已由之前的连接打开（server 重启过、忘了它）：attach 接上
                instance = str(data["bridgeInstance"])
                self._known[prepared.thread_id] = _Known(instance)
                try:
                    await self._attach(link, instance, 0)
                except (RpcFault, HostUnreachable) as exc:
                    raise _Abort(_failed("runtime_unreachable", f"接不上会话：{exc}")) from exc
                return []
            raise _Abort(_fault_intent(fault)) from fault
        except (ChannelClosed, TimeoutError) as exc:
            raise _Abort(_failed("runtime_unreachable", f"session.open 失败：{exc!r}")) from exc

        self._known[prepared.thread_id] = _Known(str(opened["bridge"]["instance"]))
        session_id = str(opened.get("agentSessionId", ""))
        if session_id and session_id != existing:
            await self._sessions.remember(prepared.thread_id, session_id)
        if existing and not opened.get("resumed"):
            # 恢复失败必须可见，不能只写日志
            return [
                (
                    EventType.SESSION_LOST,
                    {"reason": "CLI 会话未能恢复，已新建 —— 上次的上下文不在了"},
                )
            ]
        return []

    async def _reconnect(
        self, link: _Link, prepared: PreparedRun, turn: _Turn, relay: EventRelay, run_id: UUID
    ) -> bool:
        """一轮进行中断线：退避重连并 attach（§7.5）。返回 False = 断线期间用户取消了。

        ★ 不设窗口：断线只是通道断了，这一轮在 Pod 里照常进行。放弃只有确定的理由 ——
          连接被取代、会话已结束、bridge 重启过（进程内的这一轮已不存在）。
        ★ 每次重连前重新 ensure：Pod 可能被重建（地址、token 都变了）。新 Pod 上的
          bridge 实例 id 不同，attach 会得到 bridge_restarted，于是确定地收场，不会空等。
        """
        code = link.client.close_code
        if code in (CloseCode.SUPERSEDED, CloseCode.SESSION_GONE):
            why = "被另一条连接取代" if code == CloseCode.SUPERSEDED else "会话已被 bridge 结束"
            raise _Abort(_failed("runtime_unreachable", f"与 bridge 的连接关闭：{why}"))
        known = self._known[prepared.thread_id]
        delay = 0.5
        logger.warning("与 bridge 的连接断开（%s），重连中", code)
        while True:
            if await relay.is_cancelled(run_id):
                logger.info("断线期间收到取消，不再重连")
                return False
            try:
                assert prepared.spec.cli is not None
                endpoint = await self._pods.ensure(prepared.thread, prepared.spec.cli)
                link.retarget(_host_url(endpoint.url), endpoint.token)
                await link.connect()
                result = await self._attach(link, known.instance, turn.last_seq)
                logger.info("已重新接上 bridge（state=%s）", result.get("state"))
                return True
            except RpcFault as fault:
                data = fault.data if isinstance(fault.data, dict) else {}
                if data.get("cause") == "bridge_restarted":
                    # bridge 重启过：进程内的这一轮已不存在（§7.6）
                    self._known.pop(prepared.thread_id, None)
                    raise _Abort(
                        _failed("bridge_lost", "会话 Pod 里的 bridge 重启了，这一轮的进度丢失")
                    ) from fault
                raise _Abort(_fault_intent(fault)) from fault
            except Exception as exc:  # 连不上、Pod 暂不可用：都只是还没恢复，接着重试
                logger.warning("重连 bridge 失败，%.1fs 后重试：%s", delay, exc)
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
                delay = min(delay * 2, _RECONNECT_MAX_DELAY_S)

    async def _start(self, client: HostClient, prepared: PreparedRun, turn: _Turn) -> None:
        params: dict[str, Any] = {
            "turnId": turn.turn_id,
            "prompt": [{"type": "text", "text": _text_of(prepared.input_content)}],
        }
        cli = prepared.spec.cli
        if cli is not None:
            # ★ 每一轮都带：agent 每次新建 / 恢复会话都会把模式重置，bridge 据此按需重设
            params["mode"] = ACP_MODE_ID[cli.permission_mode]
        loop = asyncio.get_running_loop()
        until = loop.time() + _START_RETRY_S
        cancelled_other: set[str] = set()
        while True:
            try:
                await client.request(methods.TURN_START, params, timeout=_REQUEST_TIMEOUT_S)
                return
            except RpcFault as fault:
                data = fault.data if isinstance(fault.data, dict) else {}
                retry = (
                    fault.code == HostErrorCode.SESSION_NOT_OPEN
                    and data.get("cause") == "recovering"
                ) or fault.code == HostErrorCode.TURN_BUSY
                if not retry or loop.time() > until:
                    raise _Abort(_fault_intent(fault)) from fault
                other = data.get("turnId")
                if fault.code == HostErrorCode.TURN_BUSY and other and other not in cancelled_other:
                    # 上一轮在 bridge 那边还没结束（它的 run 已被平台判为中断）：取消它再开本轮
                    cancelled_other.add(other)
                    logger.warning("bridge 上还有进行中的一轮 %s，先取消它", other)
                    with contextlib.suppress(RpcFault, ChannelClosed, TimeoutError):
                        await client.request(
                            methods.TURN_CANCEL, {"turnId": other}, timeout=_REQUEST_TIMEOUT_S
                        )
                await asyncio.sleep(1.0)
            except (ChannelClosed, TimeoutError) as exc:
                raise _Abort(_failed("runtime_unreachable", f"turn.start 失败：{exc!r}")) from exc

    async def _on_note(
        self,
        method: str,
        params: dict[str, Any],
        prepared: PreparedRun,
        turn: _Turn,
        translator: AcpV1Translator,
    ) -> tuple[list[tuple[EventType, dict[str, Any]]], dict[str, Any] | None]:
        """一条 bridge 的通知 → (事件意图, 本轮的 outcome 或 None)。"""
        if method == methods.AGENT_UPDATE:
            # 只翻译本轮的；replay（server 没要历史）与 stray 不进事件流
            if params.get("origin") == "turn" and params.get("turnId") == turn.turn_id:
                update = params.get("update")
                return (translator.update(update) if isinstance(update, dict) else []), None
            return [], None
        if method == methods.TURN_STATE:
            if params.get("turnId") != turn.turn_id:
                return [], None
            if params.get("state") == "ended":
                return [], params.get("outcome") or {}
            mode = params.get("mode")
            if params.get("state") == "running" and isinstance(mode, dict):
                return [(EventType.AGENT_MODE, _mode_event(mode))], None
            return [], None
        if method == methods.SESSION_STATE:
            agent = params.get("agent") or {}
            if params.get("state") == "ready":
                new_id = agent.get("agentSessionId")
                if new_id:
                    await self._sessions.remember(prepared.thread_id, str(new_id))
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


# ═══════════════════════════════════ 内部 ═══════════════════════════════════


class _Abort(Exception):
    def __init__(self, intent: tuple[EventType, dict[str, Any]]) -> None:
        super().__init__(intent[1].get("message", ""))
        self.intent = intent


@dataclass
class _Known:
    """server 对一个会话的 bridge 的记忆（§7.8 的 bridgeInstance 与 lastSeq）。"""

    instance: str
    last_seq: int = 0


class _Link:
    """到 bridge 的连接，可以在一轮之内被替换（断线重连）。回调跨连接不变。"""

    def __init__(self, url: str, **kwargs: Any) -> None:
        self._url = url
        self._kwargs = kwargs
        self._client: HostClient | None = None

    def retarget(self, url: str, token: str) -> None:
        """换地址与凭据：Pod 重建后两者都会变。下一次 connect 生效。"""
        self._url = url
        self._kwargs["token"] = token

    @property
    def client(self) -> HostClient:
        assert self._client is not None
        return self._client

    async def connect(self) -> None:
        await self.close()
        client = HostClient(self._url, **self._kwargs)
        await client.__aenter__()
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.__aexit__(None, None, None)


class _Turn:
    """一轮在 server 侧的收件状态：按 seq 去重、定期 ack。"""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.queue: asyncio.Queue[tuple[Any, ...]] = asyncio.Queue()
        self.last_seq = 0
        self._acked = 0
        self._acked_at = 0.0

    def accept(self, params: Any) -> bool:
        """seq ≤ 已处理的最后一条 → 重复（补发与已收到的重叠），丢弃（§7.3）。"""
        if not isinstance(params, dict):
            return False
        seq = params.get("seq")
        if isinstance(seq, int):
            if seq <= self.last_seq:
                return False
            self.last_seq = seq
        return True

    async def ack(self, client: HostClient, *, force: bool = False) -> None:
        """每 1 s 或每 200 条确认一次，以先到者为准（§7.3）。"""
        if self.last_seq <= self._acked or client.closed:
            return
        now = asyncio.get_running_loop().time()
        due = self.last_seq - self._acked >= _ACK_EVERY or now - self._acked_at >= _ACK_INTERVAL_S
        if not (force or due):
            return
        with contextlib.suppress(ChannelClosed):
            await client.notify(methods.SESSION_ACK, {"seq": self.last_seq})
            self._acked, self._acked_at = self.last_seq, now


def _finish(
    outcome: dict[str, Any],
    translator: AcpV1Translator,
    failed_tools: dict[str, str],
    timeout: float,
) -> list[tuple[EventType, dict[str, Any]]]:
    """turn.state(ended) 的 outcome → 终止事件意图。"""
    answer = translator.answer
    response = outcome.get("response")
    usage: dict[str, Any] | None = usage_of(response)
    if usage is not None and (model := model_of(response)):
        # 遥测据此给这一轮的总用量计价（RunTrace 的 ACP 合成 Generation）；执行器只取整数字段
        usage = {**usage, "model": model}
    kind = outcome.get("kind")
    if kind == "completed":
        out: list[tuple[EventType, dict[str, Any]]] = [
            (EventType.MESSAGE_COMPLETED, {"content": answer.content()})
        ]
        # 「工具被挡下且一个字没说」不是成功收尾：CLI 报 end_turn 只表示它讲完了，不表示做成了。
        # 报成成功的话，父智能体拿到一句含糊的「没有产出」，会去排查一个不存在的问题
        if not answer.text.strip() and failed_tools:
            out.append((EventType.USAGE_UPDATED, usage or dict(_ZERO_USAGE)))
            out.append(_failed("subagent_silent", silent_failure_message(failed_tools)))
            return out
        for event_kind, data in translate_stop(
            str(outcome.get("stopReason")), usage or dict(_ZERO_USAGE)
        ):
            if event_kind is EventType.RUN_FINISHED:
                data = {**data, "text_len": len(answer.text)}
            out.append((event_kind, data))
        return out
    if kind == "cancelled":
        out = [(EventType.MESSAGE_COMPLETED, {"content": answer.content()})]
        cause = outcome.get("cause")
        if cause == "requested":
            return [*out, *translate_stop("cancelled", usage)]
        if usage is not None:
            # ★ usage 是 dict（usage_of 的返回值），不是 pydantic 模型 —— 原先调 .model_dump()，
            #   一轮因静默 / 断线 / 关闭被取消且带用量时会直接抛 AttributeError
            out.append((EventType.USAGE_UPDATED, dict(usage)))
        if cause == "deadline":
            return [*out, _failed("run_timeout", f"acp 一轮超过 {timeout:.0f}s，已取消")]
        if cause == "idle":
            return [*out, _failed("agent_stalled", "agent 长时间没有任何进展，已取消这一轮")]
        return [*out, _failed("runtime_interrupted", f"这一轮被中断（{cause}）")]
    # failed
    cause = outcome.get("cause")
    error = outcome.get("error") or {}
    if cause == "agent_error":
        acp = error.get("acp") or {}
        message = str(acp.get("message") or error.get("detail") or "agent 返回了错误")
        return [_failed("agent_error", f"CLI 报错：{message}")]
    if cause == "cancel_unanswered":
        return translate_crash("取消后 CLI 没有响应，已被终止并重启")
    if cause == "mode_unavailable":
        detail = str(error.get("detail") or "")
        message = f"CLI 无法切换到要求的权限模式，这一轮没有执行。{detail}"
        return [_failed("mode_unavailable", message)]
    if cause == "session_ended":
        return translate_crash(f"会话已被 bridge 结束（{outcome.get('detail')}）")
    return translate_crash()


def _mode_event(mode: dict[str, Any]) -> dict[str, Any]:
    """bridge 回报的模式（ACP modeId）→ agent.mode 事件（平台取值，不认识的原样）。"""

    def platform(mode_id: Any) -> str | None:
        return PLATFORM_MODE.get(mode_id, mode_id) if isinstance(mode_id, str) else None

    return {
        "requested": platform(mode.get("requested")),
        "effective": platform(mode.get("effective")),
        "degraded": bool(mode.get("degraded")),
    }


def _fault_intent(fault: RpcFault) -> tuple[EventType, dict[str, Any]]:
    data = fault.data if isinstance(fault.data, dict) else {}
    cause = str(data.get("cause", ""))
    if fault.code == HostErrorCode.AGENT_UNAVAILABLE:
        return _failed("runtime_crashed", f"CLI 起不来（{cause}）：{fault.message}")
    if fault.code == HostErrorCode.OPEN_TIMEOUT:
        return _failed("runtime_unreachable", f"打开 CLI 会话超时：{fault.message}")
    if fault.code == HostErrorCode.AGENT_ERROR:
        return _failed("agent_error", f"CLI 报错：{fault.message}")
    return _failed("runtime_unreachable", f"bridge 拒绝了请求（{fault.code} {cause}）")


def note_failed_tool(acc: dict[str, str], data: dict[str, Any]) -> None:
    """把一条 tool.failed 记进「这一轮失败了哪些工具」。键是 call_id。

    ★ 按 call_id 收而不是往列表里 append：同一次失败会来**两条** tool.failed ——
      平台挡下时自己发的那条（带名字和原因），和 CLI 随后发的终态 update（不带名字）。
      当成两次失败的后果是错误消息里多出一个占位符：真机上出现过
      「……没有给出任何结论：Write /workspace/x.txt、?」。
    ★ 后到的那条不带名字，不能让它把先到的名字覆盖成空。
    """
    call_id = str(data.get("call_id") or "")
    name = str(data.get("name") or "")
    if name or call_id not in acc:
        acc[call_id] = name or acc.get(call_id, "")


def silent_failure_message(failed_tools: dict[str, str]) -> str:
    """「工具被挡下且一个字没说」的错误文本。只列有名字的，不编占位符。"""
    named = list(dict.fromkeys(name for name in failed_tools.values() if name))
    detail = "：" + "、".join(named) if named else "。"
    return f"子智能体的工具调用被挡下且没有给出任何结论{detail}"


def _failed(kind: str, message: str) -> tuple[EventType, dict[str, Any]]:
    return EventType.RUN_FAILED, {"error_kind": kind, "message": message, "retryable": True}


def _host_url(url: str) -> str:
    """cluster 给的是 Pod 的基地址；上游协议的路径是 /host。"""
    return url.rstrip("/") + "/host" if urlparse(url).path in ("", "/") else url


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return str(content)
