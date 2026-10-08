"""StdioChannel：本进程的 stdin / stdout 作为一条按行收发的 MessageChannel。

供以子进程形式运行的 FakeAgent 使用。
"""

from __future__ import annotations

import asyncio
import sys

from atlas_jsonrpc import ChannelClosed

__all__ = ["StdioChannel"]


class StdioChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def open(cls, limit: int = 64 * 1024 * 1024) -> StdioChannel:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=limit)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        await self.send_raw(text.encode() + b"\n")

    async def send_raw(self, data: bytes) -> None:
        """原样写出字节（测试超长行等非常规输出用）。"""
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ChannelClosed("stdout 已关闭") from exc

    async def receive(self) -> str | None:
        line = await self._reader.readline()
        if not line:
            return None
        return line.decode().rstrip("\r\n")
