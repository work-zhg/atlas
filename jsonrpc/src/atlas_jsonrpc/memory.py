"""MemoryChannel —— 一对内存中的通道，用于测试（代码设计 §12.1）。"""

from __future__ import annotations

import asyncio

from .errors import ChannelClosed

__all__ = ["MemoryChannel", "memory_pair"]

_CLOSED = object()


class MemoryChannel:
    def __init__(self) -> None:
        self._inbox: asyncio.Queue[object] = asyncio.Queue()
        self._peer: MemoryChannel | None = None
        self._closed = False
        #: 已发出的全部消息，测试可直接检查
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise ChannelClosed("内存通道已关闭")
        self.sent.append(text)
        self._peer._inbox.put_nowait(text)

    async def receive(self) -> str | None:
        item = await self._inbox.get()
        if item is _CLOSED:
            self._inbox.put_nowait(_CLOSED)  # 之后的 receive 同样立即返回 None
            return None
        return item  # type: ignore[return-value]

    def close(self) -> None:
        """关闭本端：本端与对端的 receive 都会返回 None。"""
        if self._closed:
            return
        self._closed = True
        self._inbox.put_nowait(_CLOSED)
        if self._peer is not None:
            self._peer._inbox.put_nowait(_CLOSED)


def memory_pair() -> tuple[MemoryChannel, MemoryChannel]:
    a, b = MemoryChannel(), MemoryChannel()
    a._peer, b._peer = b, a
    return a, b
