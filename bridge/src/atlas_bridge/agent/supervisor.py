"""AgentSupervisor：拉起、握手、退出监视与重启节制（Bridge 设计 §5.5 · §5.6 · §6.5）。

AgentPort 的生产实现。只管进程与 ACP 握手，不知道会话与轮次：
「重启之后恢复哪个会话」由 HostSession 决定。

★ 退出只报告一次，而且只报告「意外」的退出：主动终止（restart / shutdown）与预热期间的
  退出不报告。报告发生在 stdout 读完之后 —— agent 最后的输出先于退出事件处理完。
★ 节制：窗口（默认 10 分钟）内最多重启 3 次，间隔按 1 s、5 s、30 s 递增；预热失败也计入。
  超过则抛 AgentUnavailable("agent_crash_loop")（§5.6）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

from atlas_host import FailCause
from atlas_jsonrpc import ChannelClosed

from ..clock import Clock
from ..errors import AgentUnavailable, BridgeError
from .client import AcpClient, Negotiated
from .process import AgentLaunch, AgentProcess, OversizeLine

__all__ = ["DEFAULT_POLICY", "AgentSupervisor", "RestartPolicy"]

logger = logging.getLogger(__name__)


class AgentCallbacks(Protocol):
    """agent 的输出交给谁。结构上与 session.ports.AgentEvents 相同（本层不依赖 session/）。"""

    async def on_agent_update(self, raw: dict[str, Any]) -> None: ...

    async def on_permission_request(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    def on_agent_lost(self, cause: FailCause) -> None: ...


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    max_restarts: int = 3
    window_s: float = 600
    #: 第 n 次（从 0 起）启动前等待 backoff_s[n]，超出取最后一个
    backoff_s: tuple[float, ...] = (1, 5, 30)


DEFAULT_POLICY = RestartPolicy()


class AgentSupervisor:
    def __init__(
        self,
        launch: AgentLaunch,
        clock: Clock,
        *,
        policy: RestartPolicy = DEFAULT_POLICY,
        boot_timeout_s: float = 60,
        kill_grace_s: float = 5,
        client_version: str = "2.0.0",
    ) -> None:
        self._launch = launch
        self._clock = clock
        self._policy = policy
        self._boot_timeout_s = boot_timeout_s
        self._kill_grace_s = kill_grace_s
        self._client_version = client_version

        self._events: AgentCallbacks | None = None
        self._proc: AgentProcess | None = None
        self._client: AcpClient | None = None
        self._negotiated: Negotiated | None = None
        self._watch: asyncio.Task[None] | None = None
        #: 握手完成、尚未被主动终止：此时的退出才算「意外」
        self._live = False
        self._stopping = False
        #: 窗口内每次（重新）启动的时刻
        self._starts: deque[float] = deque()
        #: 会话期间累计的重启次数（上报给 server）
        self.restarts = 0

    # ------------------------------------------------------------------ AgentPort

    def bind(self, events: AgentCallbacks) -> None:
        self._events = events

    @property
    def client(self) -> AcpClient:
        assert self._client is not None, "agent 尚未启动"
        return self._client

    @property
    def negotiated(self) -> Negotiated:
        assert self._negotiated is not None, "agent 尚未完成握手"
        return self._negotiated

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def boot(self) -> Negotiated:
        """拉起 + initialize；失败按节制重试，超过节制抛 AgentUnavailable。"""
        first = not self._starts
        while True:
            if not first:
                await self._throttle()
            first = False
            try:
                return await self._start()
            except (BridgeError, ChannelClosed, OSError, TimeoutError) as exc:
                logger.warning("agent 启动失败：%s", exc)
                await self._stop_current()

    async def restart(self, cause: str) -> Negotiated:
        """终止当前进程 → 按节制等待 → 重新启动。"""
        logger.warning("重启 agent：%s", cause)
        await self._stop_current()
        self.restarts += 1
        await self._throttle()
        while True:
            try:
                return await self._start()
            except (BridgeError, ChannelClosed, OSError, TimeoutError) as exc:
                logger.warning("agent 重启失败：%s", exc)
                await self._stop_current()
                await self._throttle()

    async def shutdown(self) -> None:
        self._stopping = True
        await self._stop_current()

    # ------------------------------------------------------------------ 内部

    async def _throttle(self) -> None:
        if self._stopping:
            raise AgentUnavailable("shutdown")
        now = self._clock.now()
        while self._starts and now - self._starts[0] >= self._policy.window_s:
            self._starts.popleft()
        # 第一次启动也记入 _starts，所以窗口内允许的启动次数 = 1 + max_restarts
        restarts_in_window = max(len(self._starts) - 1, 0)
        if restarts_in_window >= self._policy.max_restarts:
            raise AgentUnavailable("agent_crash_loop")
        backoff = self._policy.backoff_s
        await asyncio.sleep(backoff[min(restarts_in_window, len(backoff) - 1)])
        if self._stopping:
            raise AgentUnavailable("shutdown")

    async def _start(self) -> Negotiated:
        assert self._events is not None, "先 bind 再 boot"
        self._starts.append(self._clock.now())
        proc = AgentProcess(self._launch, on_oversize=self._on_oversize, on_stderr=self._on_stderr)
        await proc.start()
        client = AcpClient(
            proc,
            on_update=self._events.on_agent_update,
            on_permission=self._events.on_permission_request,
            client_version=self._client_version,
        )
        self._proc, self._client = proc, client
        self._watch = asyncio.create_task(self._watch_exit(proc, client), name="agent-watch")
        self._negotiated = await client.initialize(timeout=self._boot_timeout_s)
        if self._stopping:
            raise AgentUnavailable("shutdown")
        self._live = True
        logger.info("agent 已就绪（pid %s）", proc.pid)
        return self._negotiated

    async def _stop_current(self) -> None:
        self._live = False
        proc, watch = self._proc, self._watch
        if proc is None:
            return
        with contextlib.suppress(RuntimeError, ProcessLookupError):
            await proc.terminate(self._kill_grace_s)
        if watch is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await watch

    async def _watch_exit(self, proc: AgentProcess, client: AcpClient) -> None:
        await client.run()  # stdout 读完（agent 的全部输出都已处理）
        code = await proc.terminate(self._kill_grace_s)  # 已退出则立即返回；只关了 stdout 的也收掉
        if proc is not self._proc or not self._live:
            return  # 主动终止，或预热期间的退出（由 boot 处理）
        self._live = False
        logger.warning("agent 意外退出（退出码 %s）", code)
        assert self._events is not None
        self._events.on_agent_lost(FailCause.AGENT_EXITED)

    @staticmethod
    def _on_stderr(line: str) -> None:
        logger.info("agent stderr: %s", line)

    @staticmethod
    def _on_oversize(oversize: OversizeLine) -> None:
        logger.warning(
            "agent 输出了一行 %d 字节的消息，超过上限，已跳过；开头：%r",
            oversize.size,
            oversize.head[:120],
        )
