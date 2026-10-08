"""Outbox：bridge 发往 server 的唯一出口（Bridge 设计 §6.6 · §7.3 · §7.4；代码设计 §7.8）。

两个结构：
  · 发送队列：按放入顺序逐条发送。带 seq 的消息与请求的**响应**都走这里，所以
    「session.open 的响应排在重放的历史之后」这类顺序由一个队列保证。
  · 保留缓冲：带 seq 的消息发出后仍保留，直到 server 以 session.ack 确认；
    重连后 session.attach 从这里补发（§7.3）。它同时就是 §6.6 的有界发送缓冲。

★ put_* 是同步方法：状态转换与放入消息在同一个同步段里完成（代码设计 §6.2）。
★ 放入时就序列化成文本：补发时发送的是完全相同的字节，也便于按字节计量。
★ 响应不带 seq、不保留：它属于发出请求的那条连接，连接换了就丢弃。
★ 新连接先**暂不发送**：直到它发来第一个请求（通常是 session.attach 或 session.open）。
  否则在 attach 之前就会把缓冲倒给对方，随后补发又重复一遍。
★ 缓冲有界（默认 64 MiB）：
    连接在  → 满了就是背压：wait_writable 让读取 agent 的一方停下（§6.6）；
              持续满 30 s 由看门狗以 1011 断开（full_for）
    连接不在 → 淘汰最老的 agent.update（kind=data），区间记入 gaps；控制消息永不淘汰（§7.4）
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from atlas_host import Model
from atlas_jsonrpc import ChannelClosed, Notification, Request, encode

from ..session.ports import MessageKind

__all__ = ["Entry", "Outbox", "UpstreamSink"]

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class UpstreamSink(Protocol):
    """一条上游连接的发送端。由 UpstreamConnection 实现。"""

    async def send_text(self, text: str) -> None:
        """发送一条消息；连接已关闭时抛 ChannelClosed。"""
        ...


@dataclass(slots=True, eq=False)
class Entry:
    #: None = 响应（不带 seq、不保留）
    seq: int | None
    kind: MessageKind
    text: str
    #: 响应所属的连接；带 seq 的消息为 None（发给当时的连接）
    conn: UpstreamSink | None = None
    #: 断线期间被淘汰（仍在发送队列里的，发送任务跳过它）
    evicted: bool = False

    @property
    def size(self) -> int:
        return len(self.text)


class Outbox:
    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._now = clock or (lambda: asyncio.get_running_loop().time())
        self._seq = 0
        self._queue: deque[Entry] = deque()
        self._retained: deque[Entry] = deque()
        self._bytes = 0
        self._conn: UpstreamSink | None = None
        self._released = False
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writable = asyncio.Event()
        self._writable.set()
        self._full_since: float | None = None
        #: server 已确认处理完成的最后一条
        self.acked = 0
        #: 被淘汰、无法补发的序号区间（闭区间），按序号递增
        self.gaps: list[tuple[int, int]] = []

    # ------------------------------------------------------------------ 放入（同步）

    @property
    def last_seq(self) -> int:
        return self._seq

    def put_notification(
        self, method: str, build: Callable[[int], Model], *, kind: MessageKind
    ) -> int:
        seq = self._seq + 1
        text = encode(Notification(method, build(seq).to_wire()))
        self._seq = seq
        self._retain(Entry(seq, kind, text))
        return seq

    def put_request(self, method: str, build: Callable[[int], Model], *, request_id: int) -> int:
        seq = self._seq + 1
        text = encode(Request(request_id, method, build(seq).to_wire()))
        self._seq = seq
        self._retain(Entry(seq, "control", text))
        return seq

    def put_response(self, conn: UpstreamSink, text: str) -> None:
        """对 server 请求的响应：排在此前放入的全部消息之后发出，只发给 ``conn``。"""
        self._enqueue(Entry(None, "control", text, conn))

    def _retain(self, entry: Entry) -> None:
        self._retained.append(entry)
        self._bytes += entry.size
        self._enqueue(entry)
        self._check_capacity()

    def _enqueue(self, entry: Entry) -> None:
        self._queue.append(entry)
        self._idle.clear()
        self._wake.set()

    # ------------------------------------------------------------------ 容量：背压与淘汰

    @property
    def bytes_held(self) -> int:
        return self._bytes

    async def wait_writable(self) -> None:
        """背压：连接在且缓冲满时等待。连接不在时立即返回（放入时淘汰旧数据）。"""
        while self._conn is not None and self._bytes > self._max_bytes:
            self._writable.clear()
            await self._writable.wait()

    def full_for(self) -> float:
        """缓冲已经持续满了多久（秒）；不满为 0。"""
        return 0.0 if self._full_since is None else self._now() - self._full_since

    def _check_capacity(self) -> None:
        if self._bytes > self._max_bytes and self._conn is None:
            self._evict()
        if self._bytes > self._max_bytes:
            if self._full_since is None:
                self._full_since = self._now()
            self._writable.clear()
        else:
            self._full_since = None
            self._writable.set()

    def _evict(self) -> None:
        """从最老的 agent.update 开始淘汰，直到低于上限。控制消息永不淘汰（§7.4）。"""
        for entry in list(self._retained):
            if self._bytes <= self._max_bytes:
                break
            if entry.kind != "data":
                continue
            self._retained.remove(entry)
            self._bytes -= entry.size
            entry.evicted = True
            self._add_gap(entry.seq)  # type: ignore[arg-type]
        if self._bytes > self._max_bytes:
            logger.error("补发缓冲被控制消息占满（%d 字节），无法再淘汰", self._bytes)

    def _add_gap(self, seq: int) -> None:
        if self.gaps and self.gaps[-1][1] == seq - 1:
            self.gaps[-1] = (self.gaps[-1][0], seq)
        else:
            self.gaps.append((seq, seq))
        self.gaps.sort()

    # ------------------------------------------------------------------ 确认、连接与补发

    def ack(self, seq: int) -> None:
        """释放 ≤ seq 的保留条目。"""
        if seq <= self.acked:
            return
        self.acked = min(seq, self._seq)
        while self._retained and self._retained[0].seq <= self.acked:  # type: ignore[operator]
            self._bytes -= self._retained.popleft().size
        self.gaps = [(a, b) for a, b in self.gaps if b > self.acked]
        self._check_capacity()

    @property
    def retained(self) -> int:
        return len(self._retained)

    def bind(self, conn: UpstreamSink | None) -> None:
        """换连接或断开。新连接先暂不发送，直到 release（它发来了第一个请求）。"""
        self._conn = conn
        self._released = False
        self._check_capacity()  # 断开时可能需要淘汰；背压随之解除
        if conn is None:
            self._writable.set()
        self._wake.set()

    def release(self, conn: UpstreamSink) -> None:
        """连接发来了第一个请求：开始向它发送（未发出的消息按原顺序继续）。"""
        if conn is self._conn and not self._released:
            self._released = True
            self._wake.set()

    def resend_plan(self, last_seq: int) -> tuple[int, list[tuple[int, int]]]:
        """session.attach：server 已处理到 last_seq。返回 (补发起点, 无法补发的区间)。"""
        self.ack(last_seq)
        first = next((e.seq for e in self._retained), None)
        resend_from = first if first is not None else self._seq + 1
        gaps = [(max(a, last_seq + 1), b) for a, b in self.gaps if b > last_seq]
        return resend_from, gaps  # type: ignore[return-value]

    def rewind(self, conn: UpstreamSink, response_text: str) -> None:
        """attach 的响应之后，按 seq 顺序补发保留缓冲里的全部消息，再接着发实时消息。"""
        own_responses = [e for e in self._queue if e.seq is None and e.conn is conn]
        self._queue = deque(
            [Entry(None, "control", response_text, conn), *self._retained, *own_responses]
        )
        if conn is self._conn:
            self._released = True
        self._idle.clear()
        self._wake.set()

    @property
    def conn(self) -> UpstreamSink | None:
        return self._conn

    async def drained(self) -> None:
        """等到发送队列为空（或当前没有连接可发）。"""
        await self._idle.wait()

    # ------------------------------------------------------------------ 发送任务

    async def pump(self) -> None:
        """常驻发送任务。连接不在（或尚未 release）时停下，之后从队头继续。"""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue and self._conn is not None and self._released:
                entry, conn = self._queue[0], self._conn
                if entry.evicted or (entry.conn is not None and entry.conn is not conn):
                    self._queue.popleft()  # 已淘汰，或旧连接上请求的响应
                    continue
                try:
                    await conn.send_text(entry.text)
                except ChannelClosed:
                    if self._conn is conn:
                        self._conn = None
                    break
                # 只有本任务（和同步的 rewind）会改队头；rewind 之后队头已换，不能盲目 popleft
                if self._queue and self._queue[0] is entry:
                    self._queue.popleft()
            if not self._queue or self._conn is None or not self._released:
                self._idle.set()

    def pending(self) -> list[Entry]:
        """尚未发出的条目（测试与诊断用）。"""
        return list(self._queue)
