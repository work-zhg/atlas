"""InProcessAgent：进程内运行 FakeAgent 的 AgentPort，用于 session 层的组件测试。

生产实现是 AgentSupervisor（拉起真实进程、崩溃检测、重启节制）。这里只保留接口语义：
boot = 建通道 + initialize；通道关闭 = 进程退出 → on_agent_lost；restart = 新通道 + initialize。
重启前后是同一个 FakeAgent 对象：它的 history 相当于 agent 写在磁盘上的会话文件。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from atlas_host import FailCause
from atlas_jsonrpc.memory import MemoryChannel, memory_pair

from ..agent.client import AcpClient, Negotiated
from ..errors import AgentUnavailable
from ..session.ports import AgentEvents
from .fake_agent import FakeAgent, Script

__all__ = ["InProcessAgent"]


class InProcessAgent:
    def __init__(self, script: Script | None = None, *, max_restarts: int = 3) -> None:
        self.fake = FakeAgent(script)
        self.max_restarts = max_restarts
        self.restarts = 0
        #: 下一次 restart 之后的剧本（测试「重启后行为正常」）
        self.script_after_restart: Script | None = None
        #: 设置后 restart 会等它 set 再继续（测试「恢复期间」的行为）
        self.hold_restart: asyncio.Event | None = None
        self._events: AgentEvents | None = None
        self._client: AcpClient | None = None
        self._negotiated: Negotiated | None = None
        self._channels: tuple[MemoryChannel, MemoryChannel] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = False
        self.shutdowns = 0

    def bind(self, events: AgentEvents) -> None:
        self._events = events

    @property
    def client(self) -> AcpClient:
        assert self._client is not None, "agent 尚未 boot"
        return self._client

    @property
    def negotiated(self) -> Negotiated:
        assert self._negotiated is not None, "agent 尚未 boot"
        return self._negotiated

    async def boot(self) -> Negotiated:
        assert self._events is not None, "先 bind 再 boot"
        client_side, agent_side = memory_pair()
        self._channels = (client_side, agent_side)
        self._client = AcpClient(
            client_side,
            on_update=self._events.on_agent_update,
            on_permission=self._events.on_permission_request,
        )
        self._tasks = [
            asyncio.create_task(self.fake.serve(agent_side)),
            asyncio.create_task(self._watch()),
        ]
        self._negotiated = await self._client.initialize(timeout=5)
        return self._negotiated

    async def restart(self, cause: str) -> Negotiated:
        await self._stop()
        if self.hold_restart is not None:
            await self.hold_restart.wait()
        if self.restarts >= self.max_restarts:
            raise AgentUnavailable("agent_crash_loop")
        self.restarts += 1
        if self.script_after_restart is not None:
            self.fake.script = self.script_after_restart
        self._stopping = False
        return await self.boot()

    def crash(self) -> None:
        """模拟 agent 进程退出。"""
        assert self._channels is not None
        self._channels[1].close()

    async def shutdown(self) -> None:
        self.shutdowns += 1
        await self._stop()

    async def _stop(self) -> None:
        self._stopping = True
        if self._channels is not None:
            self._channels[0].close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _watch(self) -> None:
        assert self._client is not None and self._events is not None
        await self._client.run()
        if not self._stopping:
            self._events.on_agent_lost(FailCause.AGENT_EXITED)
