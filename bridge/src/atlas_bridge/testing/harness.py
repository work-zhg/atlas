"""session 层组件测试的脚手架：HostSession + 进程内 FakeAgent + FakeClock + RecordingOutbox。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from atlas_host import BridgeInfo, OpenParams, TurnStartParams

from ..session.host import HostSession
from .agent import InProcessAgent
from .clock import FakeClock
from .fake_agent import Script
from .outbox import RecordingOutbox

__all__ = ["SessionHarness", "until"]


async def until(pred: Callable[[], bool], timeout: float = 2) -> None:
    """让出事件循环，直到条件成立。"""

    async def poll() -> None:
        while not pred():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout)


class SessionHarness:
    def __init__(self, script: Script | None = None, **host_kw: Any) -> None:
        self.clock = FakeClock()
        self.outbox = RecordingOutbox()
        self.agent = InProcessAgent(script)
        self.host = HostSession(
            self.agent,
            self.outbox,
            self.clock,
            bridge=BridgeInfo(version="test", instance="b-1"),
            workspace="/workspace",
            **host_kw,
        )

    async def open(self, **kw: Any) -> None:
        await self.host.boot()
        await self.host.open(OpenParams(**kw))

    async def start(self, turn_id: str = "t1", **kw: Any) -> None:
        await self.host.start_turn(
            TurnStartParams(turn_id=turn_id, prompt=[{"type": "text", "text": "hi"}], **kw)
        )

    def ended(self, turn_id: str = "t1") -> dict[str, Any]:
        """这一轮的 turn.state(ended)。agent 的原始 prompt 响应从 outcome 拆到顶层 response。"""
        params = next(
            p
            for p in self.outbox.of("turn.state")
            if p["turnId"] == turn_id and p["state"] == "ended"
        )
        outcome = dict(params["outcome"])
        return {**params, "outcome": outcome, "response": outcome.pop("response", None)}

    async def turn_over(self, turn_id: str = "t1") -> dict[str, Any]:
        """等这一轮结束，返回它的 turn.state(ended)。"""
        await until(lambda: turn_id in self.host.ledger)
        return self.ended(turn_id)

    def agent_calls(self, method: str) -> list[Any]:
        return [p for m, p in self.agent.fake.received if m == method]

    async def close(self) -> None:
        await self.agent.shutdown()
