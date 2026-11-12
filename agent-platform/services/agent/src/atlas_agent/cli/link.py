"""上游连接（c4-model/agent/cli-runtime/code-cli-runtime-link.html）。

server 和会话 Pod 里的 bridge 之间那条连接，讲上游协议 atlas.host.v1。它只保证一件事：不管
断线、server 换实例接管，还是 bridge 补发和已收到的重叠，事件流都完整、不重复。做法是 bridge
发来的每条消息都带会话内连续的序号，这里按序号去重、定期确认；断线或接管后，认识这个 bridge
就 attach，从确认点之后补发；bridge 重启过就让这一轮失败，下一轮 open 恢复。

接续信息（CLI 的会话 id、bridge 实例、确认点）按会话存在 cli_session 表里（CliSession），
不放进程内存：换实例接管、server 重启之后都读得到。它运送 CLI 的内容但不理解它（内容翻译管），
也不决定权限请求怎么答（权限转发管）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse
from uuid import UUID

from atlas_engine.contracts.events import EventType
from atlas_host import CloseCode, HostErrorCode, methods
from atlas_jsonrpc import ChannelClosed, RpcFault

from ..repositories.cli_session import CliSessionRepository
from .client import HostClient, HostUnreachable
from .outcome import TurnAbort, failed, fault_intent
from .translate.acp_v1 import EventIntent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..sandbox.session_pod import PodEndpoint

__all__ = [
    "CliSession",
    "CliSessionStore",
    "DbCliSessions",
    "ReconnectPolicy",
    "SessionLink",
    "TurnInbox",
]

logger = logging.getLogger(__name__)

#: session.open（agent 启动 + 恢复）与普通请求的超时
_OPEN_TIMEOUT_S = 150.0
_REQUEST_TIMEOUT_S = 30.0
#: bridge 正在恢复 / 上一轮还没结束时，turn.start 的重试窗口
_START_RETRY_S = 60.0
#: session.ack 的节奏：每 1 s 或每 200 条，以先到者为准
_ACK_EVERY = 200
_ACK_INTERVAL_S = 1.0

#: bridge 发来的通知交给谁（权限请求之外的全部：内容更新、轮次状态、会话状态）
NoteSink = Callable[[str, Any], None]
#: bridge 发来的请求（权限请求）交给谁；各起一个任务处理，等多久都不堵住读取
RequestHandler = Callable[[str, Any], Awaitable[Any]]


# ═══════════════════════════════════ 接续信息 ═══════════════════════════════════


@dataclass
class CliSession:
    """server 对一个 CLI 会话的记忆（聚合根）：CLI 自己的会话 id（open 时恢复用），连着的是哪个
    bridge 实例，已确认到哪个序号。序号是会话级的，跨连接、跨轮连续。"""

    thread_id: UUID
    agent_session_id: str | None = None
    bridge_instance: str | None = None
    acked_seq: int = 0

    def knows(self, bridge_instance: str | None = None) -> bool:
        """认识这个 bridge 吗（不给实例号 = 认识任何一个）。"""
        if not self.bridge_instance:
            return False
        return bridge_instance is None or bridge_instance == self.bridge_instance

    def rebind(self, bridge_instance: str) -> None:
        """连上了一个新的 bridge 实例：序号从头数。"""
        self.bridge_instance, self.acked_seq = bridge_instance, 0


class CliSessionStore(Protocol):
    """cli_session 表（CliSessionRepository）的最小接口。"""

    async def remember(self, thread_id: UUID, agent_session_id: str) -> None: ...

    async def save_progress(self, thread_id: UUID, *, instance: str, acked_seq: int) -> None: ...


class DbCliSessions:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def remember(self, thread_id: UUID, agent_session_id: str) -> None:
        async with self._sessionmaker() as session:
            await CliSessionRepository(session).remember(thread_id, agent_session_id)
            await session.commit()

    async def save_progress(self, thread_id: UUID, *, instance: str, acked_seq: int) -> None:
        async with self._sessionmaker() as session:
            await CliSessionRepository(session).save_progress(
                thread_id, instance=instance, acked_seq=acked_seq
            )
            await session.commit()


# ═══════════════════════════════════ 收件与重连 ═══════════════════════════════════


class TurnInbox:
    """一轮在 server 这边的收件状态：按序号去重、定期确认。"""

    def __init__(self, last_seq: int = 0) -> None:
        #: 已处理到的序号
        self.last_seq = last_seq
        self._acked = last_seq
        self._acked_at = 0.0

    def accept(self, params: Any) -> bool:
        """seq ≤ 已处理的最后一条 → 重复（补发与已收到的重叠），丢弃。"""
        if not isinstance(params, dict):
            return False
        seq = params.get("seq")
        if isinstance(seq, int):
            if seq <= self.last_seq:
                return False
            self.last_seq = seq
        return True

    async def ack(self, client: HostClient, *, force: bool = False) -> bool:
        """每 1 s 或每 200 条确认一次，以先到者为准；一轮结束时强制确认。返回这次是否真的确认了。"""
        if self.last_seq <= self._acked or client.closed:
            return False
        now = asyncio.get_running_loop().time()
        due = self.last_seq - self._acked >= _ACK_EVERY or now - self._acked_at >= _ACK_INTERVAL_S
        if not (force or due):
            return False
        try:
            await client.notify(methods.SESSION_ACK, {"seq": self.last_seq})
        except ChannelClosed:
            return False
        self._acked, self._acked_at = self.last_seq, now
        return True


class ReconnectPolicy:
    """断线后要不要重连、隔多久：被取代和会话已结束不重连；其余指数退避，没有时间窗口 ——
    断线只是通道断了，这一轮在 Pod 里照常进行。"""

    MAX_DELAY_S = 30.0

    @staticmethod
    def refuse(close_code: int | None) -> str | None:
        """不该重连的理由；该重连返回 None。"""
        if close_code == CloseCode.SUPERSEDED:
            return "被另一条连接取代"
        if close_code == CloseCode.SESSION_GONE:
            return "会话已被 bridge 结束"
        return None

    @classmethod
    def next_delay(cls, previous: float) -> float:
        return min(previous * 2, cls.MAX_DELAY_S)


# ═══════════════════════════════════ 连接 ═══════════════════════════════════


class SessionLink:
    """到 bridge 的会话接续，一轮一个。连接可以在一轮之内被替换（断线重连），回调跨连接不变。"""

    def __init__(
        self,
        endpoint: PodEndpoint,
        session: CliSession,
        store: CliSessionStore,
        *,
        on_note: NoteSink,
        on_request: RequestHandler,
        connect_timeout_s: float,
    ) -> None:
        self._url = _host_url(endpoint.url)
        self._token = endpoint.token
        self._session = session
        self._store = store
        self._on_note = on_note
        self._on_request = on_request
        self._connect_timeout_s = connect_timeout_s
        self._client: HostClient | None = None
        self.inbox = TurnInbox()

    @property
    def client(self) -> HostClient:
        assert self._client is not None
        return self._client

    @property
    def closed(self) -> bool:
        return self._client is None or self._client.closed

    # ------------------------------------------------------------------ 接上会话

    async def join(
        self,
        *,
        mcp_servers: list[dict[str, Any]],
        limits: dict[str, float],
        takeover: bool,
        system_prompt_append: str | None = None,
    ) -> list[EventIntent]:
        """连上 bridge 并接上会话：认识这个 bridge 就 attach（从确认点之后补发），否则 open。"""
        try:
            await self._connect()
        except HostUnreachable as exc:
            raise TurnAbort(failed("runtime_unreachable", str(exc))) from exc
        session = self._session
        if session.knows():
            assert session.bridge_instance is not None
            try:
                await self._attach(session.bridge_instance, session.acked_seq)
                self.inbox = TurnInbox(session.acked_seq)
                return []
            except RpcFault as fault:
                if takeover:
                    # ★ 接管时接不回去 = bridge 重启过，那一轮的进度已不在。不能 open 后
                    #   重新 turn.start —— 那会把同一句话再交给 CLI 做一遍
                    raise TurnAbort(
                        failed("bridge_lost", "会话 Pod 里的 bridge 重启了，这一轮的进度丢失")
                    ) from fault
                if fault.code != HostErrorCode.SESSION_NOT_OPEN:
                    raise TurnAbort(fault_intent(fault)) from fault
                # bridge 重启过（或还没打开过会话）：它的进程内状态已不在，重新 open 恢复
                logger.info("bridge 不认识这个会话了（%s），改为 session.open", fault.data)
        return await self._open(mcp_servers, limits, system_prompt_append)

    async def _open(
        self,
        mcp_servers: list[dict[str, Any]],
        limits: dict[str, float],
        system_prompt_append: str | None = None,
    ) -> list[EventIntent]:
        existing = self._session.agent_session_id
        params: dict[str, Any] = {"mcpServers": mcp_servers, "defaults": limits}
        if system_prompt_append:
            # 知识集目录（知识集方案 §3.4）：bridge 经 _meta.systemPrompt.append 交给 adapter
            params["systemPromptAppend"] = system_prompt_append
        if existing:
            params["resume"] = {"agentSessionId": existing, "replay": "none"}
        try:
            opened = await self.client.request(
                methods.SESSION_OPEN, params, timeout=_OPEN_TIMEOUT_S
            )
        except RpcFault as fault:
            data = fault.data if isinstance(fault.data, dict) else {}
            if fault.code == HostErrorCode.SESSION_ALREADY_OPEN and data.get("bridgeInstance"):
                # Pod 上的会话已由之前的连接打开（库里没记下它）：attach 接上
                instance = str(data["bridgeInstance"])
                try:
                    await self._attach(instance, 0)
                except (RpcFault, HostUnreachable) as exc:
                    raise TurnAbort(failed("runtime_unreachable", f"接不上会话：{exc}")) from exc
                self._session.rebind(instance)
                self.inbox = TurnInbox()
                return []
            raise TurnAbort(fault_intent(fault)) from fault
        except (ChannelClosed, TimeoutError) as exc:
            raise TurnAbort(failed("runtime_unreachable", f"session.open 失败：{exc!r}")) from exc

        self._session.rebind(str(opened["bridge"]["instance"]))
        self.inbox = TurnInbox()
        session_id = str(opened.get("agentSessionId", ""))
        if session_id and session_id != existing:
            await self.remember(session_id)
        if existing and not opened.get("resumed"):
            # 恢复失败必须可见，不能只写日志
            return [
                (
                    EventType.SESSION_LOST,
                    {"reason": "CLI 会话未能恢复，已新建 —— 上次的上下文不在了"},
                )
            ]
        return []

    async def _attach(self, instance: str, last_seq: int) -> dict[str, Any]:
        try:
            result = await self.client.request(
                methods.SESSION_ATTACH,
                {"bridgeInstance": instance, "lastSeq": last_seq},
                timeout=_REQUEST_TIMEOUT_S,
            )
        except (ChannelClosed, TimeoutError) as exc:
            raise HostUnreachable(f"session.attach 失败：{exc!r}") from exc
        if result.get("gaps"):
            # 补发不完整：缺的只是一段展示内容，会话与轮次的状态仍然完整
            logger.warning("bridge 的补发缓冲溢出过，缺失区间 %s", result["gaps"])
        return result  # type: ignore[no-any-return]

    async def remember(self, agent_session_id: str) -> None:
        """CLI 自己的会话 id 变了（新建 / 重启后恢复）：记下，下一次 open 时带上。"""
        self._session.agent_session_id = agent_session_id
        await self._store.remember(self._session.thread_id, agent_session_id)

    # ------------------------------------------------------------------ 一轮

    async def start_turn(self, turn_id: str, prompt: str, mode: str | None) -> None:
        """开一轮。turnId 就是 run id：重复提交安全，bridge 按幂等处理。"""
        params: dict[str, Any] = {"turnId": turn_id, "prompt": [{"type": "text", "text": prompt}]}
        if mode is not None:
            params["mode"] = mode
        loop = asyncio.get_running_loop()
        until = loop.time() + _START_RETRY_S
        cancelled_other: set[str] = set()
        while True:
            try:
                await self.client.request(methods.TURN_START, params, timeout=_REQUEST_TIMEOUT_S)
                return
            except RpcFault as fault:
                data = fault.data if isinstance(fault.data, dict) else {}
                retry = (
                    fault.code == HostErrorCode.SESSION_NOT_OPEN
                    and data.get("cause") == "recovering"
                ) or fault.code == HostErrorCode.TURN_BUSY
                if not retry or loop.time() > until:
                    raise TurnAbort(fault_intent(fault)) from fault
                other = data.get("turnId")
                if fault.code == HostErrorCode.TURN_BUSY and other and other not in cancelled_other:
                    # 上一轮在 bridge 那边还没结束（它的 run 已被平台判为中断）：取消它再开本轮
                    cancelled_other.add(other)
                    logger.warning("bridge 上还有进行中的一轮 %s，先取消它", other)
                    await self.cancel_turn(other)
                await asyncio.sleep(1.0)
            except (ChannelClosed, TimeoutError) as exc:
                raise TurnAbort(failed("runtime_unreachable", f"turn.start 失败：{exc!r}")) from exc

    async def cancel_turn(self, turn_id: str) -> None:
        """请 bridge 取消这一轮；取消的结果以随后的 turn.state(ended) 为准。"""
        with contextlib.suppress(RpcFault, ChannelClosed, TimeoutError):
            await self.client.request(
                methods.TURN_CANCEL, {"turnId": turn_id}, timeout=_REQUEST_TIMEOUT_S
            )

    async def acknowledge(self, *, force: bool = False) -> None:
        """确认已处理到的序号，确认了就写进 cli_session（接管的实例据此 attach）。

        写库失败只记日志：最坏是接管时多补发一段（按序号去重）。
        """
        if self._client is None or not await self.inbox.ack(self._client, force=force):
            return
        await self.save_progress()

    async def save_progress(self) -> None:
        session = self._session
        if not session.bridge_instance:
            return
        session.acked_seq = self.inbox.last_seq
        try:
            await self._store.save_progress(
                session.thread_id, instance=session.bridge_instance, acked_seq=session.acked_seq
            )
        except Exception:
            logger.warning("cli_session 的进度写库失败 thread=%s", session.thread_id, exc_info=True)

    # ------------------------------------------------------------------ 断线

    async def reconnect(
        self,
        reach: Callable[[], Awaitable[PodEndpoint]],
        cancelled: Callable[[], Awaitable[bool]],
    ) -> bool:
        """一轮进行中断线：退避重连并 attach。返回 False = 断线期间用户取消了。

        ★ 不设窗口：断线只是通道断了，这一轮在 Pod 里照常进行。放弃只有确定的理由 ——
          连接被取代、会话已结束、bridge 重启过（进程内的这一轮已不存在）。
        ★ 每次重连前重新 reach（确保 Pod）：Pod 可能被重建（地址、token 都变了）。新 Pod 上的
          bridge 实例 id 不同，attach 会得到 bridge_restarted，于是确定地收场，不会空等。
        """
        code = self._client.close_code if self._client is not None else None
        if (why := ReconnectPolicy.refuse(code)) is not None:
            raise TurnAbort(failed("runtime_unreachable", f"与 bridge 的连接关闭：{why}"))
        instance = self._session.bridge_instance
        assert instance is not None
        delay = 0.5
        logger.warning("与 bridge 的连接断开（%s），重连中", code)
        while True:
            if await cancelled():
                logger.info("断线期间收到取消，不再重连")
                return False
            try:
                endpoint = await reach()
                self._url, self._token = _host_url(endpoint.url), endpoint.token
                await self._connect()
                result = await self._attach(instance, self.inbox.last_seq)
                logger.info("已重新接上 bridge（state=%s）", result.get("state"))
                return True
            except RpcFault as fault:
                data = fault.data if isinstance(fault.data, dict) else {}
                if data.get("cause") == "bridge_restarted":
                    raise TurnAbort(
                        failed("bridge_lost", "会话 Pod 里的 bridge 重启了，这一轮的进度丢失")
                    ) from fault
                raise TurnAbort(fault_intent(fault)) from fault
            except Exception as exc:  # 连不上、Pod 暂不可用：都只是还没恢复，接着重试
                logger.warning("重连 bridge 失败，%.1fs 后重试：%s", delay, exc)
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))
                delay = ReconnectPolicy.next_delay(delay)

    # ------------------------------------------------------------------ 连接本身

    async def _connect(self) -> None:
        await self.close()

        async def on_notification(method: str, params: Any) -> None:
            self._on_note(method, params)

        client = HostClient(
            self._url,
            token=self._token,
            session_id=str(self._session.thread_id),
            on_notification=on_notification,
            on_request=self._on_request,
            connect_timeout_s=self._connect_timeout_s,
        )
        await client.__aenter__()
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.__aexit__(None, None, None)


def _host_url(url: str) -> str:
    """PodProvider 给的是 Pod 的基地址；上游协议的路径是 /host。"""
    return url.rstrip("/") + "/host" if urlparse(url).path in ("", "/") else url
