"""SSE（server-sent events）的读和写：按行读，空行分隔一个事件，冒号开头的是注释。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

__all__ = ["KEEPALIVE", "SSEEvent", "iter_sse"]

#: 保活注释：SSE 规定接收方忽略冒号开头的行，只让中间的代理别因为太久没动静断开连接
KEEPALIVE = b": keepalive\n\n"


@dataclass
class SSEEvent:
    event: str | None
    data: str
    _parsed: Any = None

    def json(self) -> Any:
        if self._parsed is None:
            self._parsed = json.loads(self.data)
        return self._parsed

    @property
    def done(self) -> bool:
        """OpenAI 格式的结束标记。"""
        return self.data.strip() == "[DONE]"

    def encode(self) -> bytes:
        head = f"event: {self.event}\n" if self.event else ""
        body = "\n".join(f"data: {line}" for line in self.data.split("\n"))
        return f"{head}{body}\n\n".encode()

    @classmethod
    def of(cls, data: Any, event: str | None = None) -> SSEEvent:
        text = (
            data
            if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )
        return cls(event, text, None if isinstance(data, str) else data)


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    event: str | None = None
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield SSEEvent(event, "\n".join(data))
