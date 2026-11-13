"""多路复用：收发、关闭原因、控流、卡住只关一路、优先级、连接断开。"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from atlas_devicelink import (
    FILE,
    GOING_AWAY,
    INTERNAL_ERROR,
    SESSION,
    Channel,
    ChannelStalled,
    Data,
    Mux,
    decode,
    encode,
)


class Pipe:
    """内存里的一端：发出去的进对端的收件箱。"""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.peer: Pipe | None = None
        self.sent: list[str | bytes] = []
        self.closed = False
        self.gate: asyncio.Event | None = None  # 设上就卡住发送（模拟慢连接）

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise ConnectionError("closed")
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(message)
        assert self.peer is not None
        self.peer.inbox.put_nowait(message)

    async def receive(self) -> str | bytes | None:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.inbox.put_nowait(None)
        if self.peer is not None and not self.peer.closed:
            self.peer.closed = True
            self.peer.inbox.put_nowait(None)


def pair() -> tuple[Pipe, Pipe]:
    a, b = Pipe(), Pipe()
    a.peer, b.peer = b, a
    return a, b


@contextlib.asynccontextmanager
async def linked(**kwargs):
    a, b = pair()
    accepted: asyncio.Queue[Channel] = asyncio.Queue()

    async def on_open(channel: Channel) -> None:
        await accepted.put(channel)

    gw = Mux(a, opener=True, **kwargs)
    dev = Mux(b, opener=False, on_open=on_open, **kwargs)
    tasks = [asyncio.create_task(gw.run()), asyncio.create_task(dev.run())]
    try:
        yield gw, dev, accepted, (a, b)
    finally:
        await gw.close()
        await asyncio.wait(tasks, timeout=2)


async def test_open_send_receive_both_ways() -> None:
    async with linked() as (gw, _dev, accepted, _):
        ch = await gw.open(SESSION, {"thread": "t1"})
        peer = await asyncio.wait_for(accepted.get(), 1)
        assert peer.kind == SESSION and peer.head == {"thread": "t1"}
        await ch.send('{"jsonrpc":"2.0"}')
        assert await asyncio.wait_for(peer.receive(), 1) == '{"jsonrpc":"2.0"}'
        await peer.send(b"\x00\x01binary")
        assert await asyncio.wait_for(ch.receive(), 1) == b"\x00\x01binary"


async def test_close_reason_reaches_the_other_side() -> None:
    async with linked() as (gw, _dev, accepted, _):
        ch = await gw.open(SESSION)
        peer = await asyncio.wait_for(accepted.get(), 1)
        await peer.close(4410, "session ended")
        assert await asyncio.wait_for(ch.receive(), 1) is None
        assert (ch.close_code, ch.close_reason) == (4410, "session ended")
        assert ch.id not in gw.channels


async def test_losing_the_connection_closes_every_channel() -> None:
    a, b = pair()
    gw = Mux(a, opener=True)
    task = asyncio.create_task(gw.run())
    ch1 = await gw.open(SESSION)
    ch2 = await gw.open(FILE)
    await b.close()
    await asyncio.wait_for(task, 1)
    for ch in (ch1, ch2):
        assert ch.closed and ch.close_code == GOING_AWAY
        assert await ch.receive() is None


async def test_window_limits_unread_bytes_and_reading_returns_it() -> None:
    async with linked(window=100) as (gw, _dev, accepted, _):
        ch = await gw.open(FILE)
        peer = await asyncio.wait_for(accepted.get(), 1)
        for _ in range(3):  # 3 × 40 字节：用掉 100 的窗口（第三条透支），第四条要等
            await asyncio.wait_for(ch.send(b"x" * 40), 1)
        blocked = asyncio.create_task(ch.send(b"x" * 40))
        await asyncio.sleep(0.05)
        assert not blocked.done()
        for _ in range(3):
            await peer.receive()  # 读够一半窗口就还
        await asyncio.wait_for(blocked, 1)


async def test_stalled_channel_is_closed_alone() -> None:
    async with linked(window=10, stall_s=0.1) as (gw, _dev, accepted, _):
        stuck = await gw.open(FILE)
        other = await gw.open(SESSION)
        await asyncio.wait_for(accepted.get(), 1)
        peer_other = await asyncio.wait_for(accepted.get(), 1)
        await stuck.send(b"x" * 10)
        with pytest.raises(ChannelStalled):
            await stuck.send(b"x")  # 对端一直不读
        assert stuck.closed and stuck.close_code == INTERNAL_ERROR
        await other.send("still fine")
        assert await asyncio.wait_for(peer_other.receive(), 1) == "still fine"


async def test_session_frames_jump_ahead_of_queued_file_frames() -> None:
    a, b = pair()
    gw = Mux(a, opener=True, window=1 << 30)
    task = asyncio.create_task(gw.run())
    file_ch = await gw.open(FILE)
    session_ch = await gw.open(SESSION)
    a.gate = asyncio.Event()  # 卡住发送，让帧在队列里排
    sends = [asyncio.create_task(file_ch.send(b"f" * 10)) for _ in range(3)]
    await asyncio.sleep(0)
    sends.append(asyncio.create_task(session_ch.send("s")))
    await asyncio.sleep(0.01)
    a.gate.set()
    await asyncio.wait_for(asyncio.gather(*sends), 1)
    data = [decode(m) for m in a.sent if isinstance(m, bytes)]
    # 第一块文件内容可能已经在发送中，会话消息排在剩下的文件块前面
    order = [d.ch for d in data if isinstance(d, Data)]
    assert order.index(session_ch.id) <= 1
    await b.close()
    await asyncio.wait_for(task, 1)


async def test_bad_frame_closes_the_connection() -> None:
    a, b = pair()
    dev = Mux(b, opener=False, on_open=None)
    task = asyncio.create_task(dev.run())
    await a.send("not json")
    await asyncio.wait_for(task, 1)
    assert b.closed


async def test_side_without_on_open_refuses_channels() -> None:
    a, b = pair()
    gw = Mux(a, opener=True)
    dev = Mux(b, opener=False)
    tasks = [asyncio.create_task(gw.run()), asyncio.create_task(dev.run())]
    ch = await gw.open(SESSION)
    assert await asyncio.wait_for(ch.receive(), 1) is None
    assert ch.close_code == 4400
    await gw.close()
    await asyncio.wait(tasks, timeout=1)


def test_frames_round_trip() -> None:
    for message in (encode(Data(7, "文本")), encode(Data(7, b"\xff\x00"))):
        assert encode(decode(message)) == message
