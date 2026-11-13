"""一条连接上的多路复用。

对应 c4-model/agent/devicegw/code-devicegw-link.html 的 DeviceLink / Channel / SendScheduler。

    Mux       一条连接：读循环按通道号分发帧，发送按优先级排队
    Channel   其中一路：会话流（session）或文件流（file）

★ 控流按字节、按通道：对端给多少窗口才发多少，读走了再把窗口还回去。多路共用一条 TCP 连接，没有窗口
  的话一路读得慢就会堵住整条连接。一条消息比剩下的窗口大也照发（会话流里的单条消息可能有几 MB），
  欠下的窗口从之后还回来的里扣。
★ 发送顺序：控制帧最先，会话流其次，文件流最后。文件内容由调用方分成小块，排在会话消息后面穿插着发，
  下载大文件不会让对话卡住。
★ 一路的窗口迟迟不还（对端卡住），超过 stall_s 就只关这一路（1011），其余照常。会话流被关后
  靠 bridge 的断线补发恢复，文件请求重试即可。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any, Protocol

from .frames import Close, Credit, Data, FrameError, Open, decode, encode

__all__ = [
    "Channel",
    "ChannelClosed",
    "ChannelStalled",
    "Mux",
    "Priority",
    "Transport",
]

logger = logging.getLogger(__name__)

#: 关闭码（与 WebSocket 的含义一致，原样传给另一端）
NORMAL = 1000
GOING_AWAY = 1001
INTERNAL_ERROR = 1011
PROTOCOL_VIOLATION = 4400


class Transport(Protocol):
    """一条 WebSocket 连接的最小接口：两边各自把自己的 WebSocket 库适配成它。"""

    async def send(self, message: str | bytes) -> None: ...

    async def receive(self) -> str | bytes | None:
        """下一条消息；连接已经关了返回 None。"""
        ...

    async def close(self, code: int = NORMAL, reason: str = "") -> None: ...


class Priority(IntEnum):
    CONTROL = 0
    SESSION = 1
    FILE = 2


#: 通道类型 → 发送优先级
KIND_PRIORITY = {"session": Priority.SESSION, "file": Priority.FILE}


class ChannelClosed(Exception):
    """通道已经关了（本端或对端），code / reason 是关闭原因。"""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"通道已关闭（{code} {reason}）")
        self.code = code
        self.reason = reason


class ChannelStalled(ChannelClosed):
    """对端长时间不还窗口，这一路被关了。"""


OnOpen = Callable[["Channel"], Awaitable[None]]


class Channel:
    """一条通道。发送受对端给的窗口约束；收到的消息读走后把窗口还回去。"""

    def __init__(
        self,
        mux: Mux,
        ch: int,
        kind: str,
        head: dict[str, Any],
        *,
        window: int,
        stall_s: float,
    ) -> None:
        self._mux = mux
        self.id = ch
        self.kind = kind
        self.head = head
        self._window = window
        self._stall_s = stall_s
        self._credit = window
        self._credit_changed = asyncio.Event()
        self._inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._unacked = 0
        self.closed = False
        self.close_code: int | None = None
        self.close_reason = ""

    @property
    def priority(self) -> Priority:
        return KIND_PRIORITY.get(self.kind, Priority.FILE)

    # ------------------------------------------------------------------ 发送

    async def send(self, payload: str | bytes) -> None:
        """发一条消息。窗口用完就等对端还；等太久这一路被关（ChannelStalled）。"""
        if self.closed:
            raise ChannelClosed(self.close_code or NORMAL, self.close_reason)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stall_s
        while self._credit <= 0:
            self._credit_changed.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self.close(INTERNAL_ERROR, "对端长时间不读，这一路被关闭")
                raise ChannelStalled(INTERNAL_ERROR, "stalled")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._credit_changed.wait(), remaining)
            if self.closed:
                raise ChannelClosed(self.close_code or NORMAL, self.close_reason)
        self._credit -= _size(payload)
        await self._mux._send(encode(Data(self.id, payload)), self.priority)

    # ------------------------------------------------------------------ 接收

    async def receive(self) -> str | bytes | None:
        """下一条消息；通道关了返回 None（之后一直返回 None）。"""
        item = await self._inbox.get()
        if item is None:
            self._inbox.put_nowait(None)
            return None
        self._unacked += _size(item)
        if self._unacked >= self._window // 2 and not self.closed:
            n, self._unacked = self._unacked, 0
            with contextlib.suppress(ConnectionError):
                await self._mux._send(encode(Credit(self.id, n)), Priority.CONTROL)
        return item

    def __aiter__(self) -> Channel:
        return self

    async def __anext__(self) -> str | bytes:
        item = await self.receive()
        if item is None:
            raise StopAsyncIteration
        return item

    # ------------------------------------------------------------------ 关闭

    async def close(self, code: int = NORMAL, reason: str = "") -> None:
        """关这一路，并把原因告诉对端。重复调用无害。"""
        if self.closed:
            return
        self._mark_closed(code, reason)
        with contextlib.suppress(ConnectionError):
            await self._mux._send(encode(Close(self.id, code, reason)), Priority.CONTROL)

    def _mark_closed(self, code: int, reason: str) -> None:
        self.closed = True
        self.close_code, self.close_reason = code, reason
        self._mux._forget(self.id)
        self._inbox.put_nowait(None)
        self._credit_changed.set()

    # ------------------------------------------------------------------ 读循环调用

    def _deliver(self, payload: str | bytes) -> None:
        if not self.closed:
            self._inbox.put_nowait(payload)

    def _grant(self, n: int) -> None:
        self._credit += n
        self._credit_changed.set()


class Mux:
    """一条连接上的多路复用。run() 跑读循环和发送循环，连接断了就返回，上面的通道全部关闭。

    opener=True 的一端开通道（设备网关），另一端在 on_open 里接（连接器）。两端都能开也不会撞号：
    开的一端用奇数，另一端用偶数。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        opener: bool,
        on_open: OnOpen | None = None,
        window: int = 1 << 20,
        stall_s: float = 30.0,
    ) -> None:
        self._transport = transport
        self._on_open = on_open
        self._window = window
        self._stall_s = stall_s
        self._ids = itertools.count(1 if opener else 2, 2)
        self._channels: dict[int, Channel] = {}
        self._queue: asyncio.PriorityQueue[tuple[int, int, str | bytes, asyncio.Future[None]]] = (
            asyncio.PriorityQueue()
        )
        self._seq = itertools.count()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._done = asyncio.Event()
        #: 连接断开时给每路的关闭原因（调用方可以改，比如被新连接取代）
        self.lost_code = GOING_AWAY
        self.lost_reason = "连接断开"

    @property
    def channels(self) -> dict[int, Channel]:
        return dict(self._channels)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    async def open(self, kind: str, head: dict[str, Any] | None = None) -> Channel:
        """开一路。对端在 on_open 里收到它；之后两边各自 send / receive。"""
        if self._done.is_set():
            raise ConnectionError("连接已断开")
        ch = next(self._ids)
        channel = self._channel(ch, kind, head or {})
        await self._send(encode(Open(ch, kind, channel.head)), Priority.CONTROL)
        return channel

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_loop())
        try:
            await self._read_loop()
        finally:
            self._done.set()
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            self._fail_pending()
            for channel in list(self._channels.values()):
                channel._mark_closed(self.lost_code, self.lost_reason)
            for task in list(self._tasks):
                task.cancel()

    async def close(self, code: int = NORMAL, reason: str = "") -> None:
        """关掉整条连接：上面的通道都以这个原因关闭。"""
        self.lost_code, self.lost_reason = code, reason
        with contextlib.suppress(Exception):
            await self._transport.close(code, reason)

    # ------------------------------------------------------------------ 内部

    def _channel(self, ch: int, kind: str, head: dict[str, Any]) -> Channel:
        channel = Channel(self, ch, kind, head, window=self._window, stall_s=self._stall_s)
        self._channels[ch] = channel
        return channel

    def _forget(self, ch: int) -> None:
        self._channels.pop(ch, None)

    async def _send(self, message: str | bytes, priority: Priority) -> None:
        if self._done.is_set():
            raise ConnectionError("连接已断开")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((int(priority), next(self._seq), message, future))
        await future

    async def _send_loop(self) -> None:
        while True:
            _, _, message, future = await self._queue.get()
            try:
                await self._transport.send(message)
            except Exception as exc:  # 连接断了：等读循环收尾
                if not future.done():
                    future.set_exception(ConnectionError(str(exc)))
                continue
            if not future.done():
                future.set_result(None)

    def _fail_pending(self) -> None:
        while not self._queue.empty():
            *_, future = self._queue.get_nowait()
            if not future.done():
                future.set_exception(ConnectionError("连接已断开"))

    async def _read_loop(self) -> None:
        while True:
            try:
                message = await self._transport.receive()
            except Exception:
                logger.info("控制连接读失败", exc_info=True)
                return
            if message is None:
                return
            try:
                frame = decode(message)
            except FrameError as exc:
                logger.warning("对端发来不合格式的帧：%s", exc)
                await self.close(PROTOCOL_VIOLATION, str(exc))
                return
            match frame:
                case Data(ch=ch, payload=payload):
                    if (channel := self._channels.get(ch)) is not None:
                        channel._deliver(payload)
                case Credit(ch=ch, n=n):
                    if (channel := self._channels.get(ch)) is not None:
                        channel._grant(n)
                case Close(ch=ch, code=code, reason=reason):
                    if (channel := self._channels.get(ch)) is not None:
                        channel._mark_closed(code, reason)
                case Open(ch=ch, kind=kind, head=head):
                    if ch in self._channels:
                        continue
                    channel = self._channel(ch, kind, head)
                    if self._on_open is None:
                        await channel.close(PROTOCOL_VIOLATION, "这一端不接受开通道")
                        continue
                    self._spawn(self._on_open(channel))

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def guarded() -> None:
            try:
                await coro
            except Exception:
                logger.exception("处理通道时出错")

        task = asyncio.create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _size(payload: str | bytes) -> int:
    return len(payload.encode()) if isinstance(payload, str) else len(payload)
