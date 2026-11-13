"""连接器测试的辅助：内存里的一对多路复用、假适配器的命令、策略。"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from atlas_bridge.testing.fake_agent import Script
from atlas_connect.config import CliCommand, LocalPolicy
from atlas_devicelink import Channel, Mux


class Pipe:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.peer: Pipe | None = None
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("closed")
        assert self.peer is not None
        self.peer.inbox.put_nowait(message)

    async def receive(self) -> str | bytes | None:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(None)
            if self.peer is not None and not self.peer.closed:
                self.peer.closed = True
                self.peer.inbox.put_nowait(None)


@contextlib.asynccontextmanager
async def linked(on_open: Callable[[Channel], Awaitable[None]]) -> AsyncIterator[Mux]:
    """网关一侧的 Mux（开通道）；连接器一侧把通道交给 on_open。"""
    a, b = Pipe(), Pipe()
    a.peer, b.peer = b, a
    gw = Mux(a, opener=True)
    dev = Mux(b, opener=False, on_open=on_open)
    tasks = [asyncio.create_task(gw.run()), asyncio.create_task(dev.run())]
    try:
        yield gw
    finally:
        await gw.close()
        await asyncio.wait(tasks, timeout=2)


def fake_agent(script: Script | None = None) -> tuple[str, ...]:
    return (sys.executable, "-m", "atlas_bridge.testing.fake_agent", (script or Script()).to_json())


def policy(root: Path, **kw: object) -> LocalPolicy:
    return LocalPolicy(
        allowed_roots=(str(root),),
        clis={"claude-code": CliCommand(fake_agent())},
        **kw,  # type: ignore[arg-type]
    )
