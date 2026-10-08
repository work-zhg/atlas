"""UpstreamProbe：测试用的上游客户端（代码设计 §12.1）。扮演 server：发指令、收消息、答复询问。"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Callable
from typing import Any

from atlas_host import HEADER_SESSION, SUBPROTOCOL_V1
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

__all__ = ["RpcError", "UpstreamProbe"]


class RpcError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(f"[{error.get('code')}] {error.get('message')}")
        self.code: int = error.get("code", 0)
        self.data: Any = error.get("data")


class UpstreamProbe:
    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        #: bridge 发来的全部通知与请求，按到达顺序
        self.inbox: list[dict[str, Any]] = []
        #: 全部帧（含响应），按到达顺序 —— 检查「响应排在重放之后」这类顺序
        self.frames: list[dict[str, Any]] = []
        self._ids = itertools.count(1000)
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._changed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())
        self.close_code: int | None = None

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        token: str,
        session_id: str,
        subprotocols: tuple[str, ...] = (SUBPROTOCOL_V1,),
    ) -> UpstreamProbe:
        ws = await connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}", HEADER_SESSION: session_id},
            subprotocols=list(subprotocols),  # type: ignore[arg-type]
            compression=None,
            max_size=None,
        )
        return cls(ws)

    async def _read(self) -> None:
        try:
            async for message in self.ws:
                frame = json.loads(message)
                self.frames.append(frame)
                if "method" in frame:
                    self.inbox.append(frame)
                else:
                    future = self._waiting.pop(frame["id"], None)
                    if future is not None and not future.done():
                        future.set_result(frame)
                self._changed.set()
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self.ws.close_code
            self._changed.set()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        rid = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[rid] = future
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        )
        frame = await asyncio.wait_for(future, 10)
        if "error" in frame:
            raise RpcError(frame["error"])
        return frame["result"]

    async def respond(self, request_id: int, result: Any) -> None:
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}))

    async def ack(self, seq: int) -> None:
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "method": "session.ack", "params": {"seq": seq}})
        )

    async def wait_for(
        self, pred: Callable[[list[dict[str, Any]]], Any], timeout: float = 10
    ) -> Any:
        """等到 ``pred(inbox)`` 为真，返回它的值。"""

        async def poll() -> Any:
            while True:
                value = pred(self.inbox)
                if value:
                    return value
                if self.close_code is not None or self._reader.done():
                    raise ConnectionError(f"连接已关闭（{self.close_code}）")
                self._changed.clear()
                await self._changed.wait()

        return await asyncio.wait_for(poll(), timeout)

    def of(self, method: str) -> list[dict[str, Any]]:
        return [m["params"] for m in self.inbox if m["method"] == method]

    async def turn_ended(self, turn_id: str, timeout: float = 10) -> dict[str, Any]:
        def ended(inbox: list[dict[str, Any]]) -> Any:
            for m in inbox:
                p = m.get("params", {})
                if m["method"] == "turn.state" and p["turnId"] == turn_id and p["state"] == "ended":
                    return p
            return None

        return await self.wait_for(ended, timeout)

    async def closed(self, timeout: float = 10) -> int | None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._reader), timeout)
        return self.close_code

    async def close(self) -> None:
        await self.ws.close()
        with contextlib.suppress(Exception):
            await self._reader
