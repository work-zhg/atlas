"""本机控制：命令行经 Unix socket（~/.atlas/run/connect.sock）问正在跑的连接器。

    {"cmd": "status"}                   → 连接状态、正在跑的会话
    {"cmd": "stop", "thread": "前缀"}   → 结束会话

★ socket 只有本人可读写（0600）：电脑主人随时能看到、能停，别人不行。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from .link import Connector
from .session import SessionHost

__all__ = ["ControlServer", "ask"]


class ControlServer:
    def __init__(self, path: Path, host: SessionHost, connector: Connector, *, device: str) -> None:
        self._path = path
        self._host = host
        self._connector = connector
        self._device = device
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._path))
        os.chmod(self._path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await reader.readline() or b"{}")
            writer.write(json.dumps(await self._answer(req), ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    async def _answer(self, req: dict[str, Any]) -> dict[str, Any]:
        if req.get("cmd") == "status":
            return {
                "device": self._device,
                "state": self._connector.state,
                "sessions": self._host.list(),
            }
        if req.get("cmd") == "stop":
            return {"stopped": await self._host.stop(str(req.get("thread") or "-"))}
        return {"error": f"不认识的命令：{req.get('cmd')}"}


async def ask(path: Path, req: dict[str, Any]) -> dict[str, Any] | None:
    """问正在跑的连接器；没在跑返回 None。"""
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    try:
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())  # type: ignore[no-any-return]
    finally:
        writer.close()
