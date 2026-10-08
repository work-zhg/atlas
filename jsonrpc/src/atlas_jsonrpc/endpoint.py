"""RpcEndpoint —— 一条通道上的 JSON-RPC 对端。

双向：既能向对方发请求与通知，也能处理对方发来的请求与通知。与传输无关，
只依赖 ``MessageChannel``。

行为约定（代码设计 §2）：
  · 对方的**请求**：每个起一个任务处理，读循环从不等待处理完成 ——
    一个要等很久的请求（例如等人决定的权限请求）不能堵住后续的读取。
  · 对方的**通知**：在读循环里按到达顺序**串行**处理。顺序对流式更新有意义；
    处理函数若在等待（例如等发送缓冲腾出空间），读取随之暂停 —— 这就是背压。
  · 收到的**响应**：按 id 唤醒挂起的请求；找不到的记日志后丢弃。
  · **通道关闭**：挂起的请求全部以 ``ChannelClosed`` 失败，进行中的请求处理任务全部取消。
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .channel import MessageChannel
from .errors import ChannelClosed, ErrorCode, RpcFault
from .frames import InvalidFrame, Notification, Request, RequestId, Response, encode, parse_frame

__all__ = ["NotificationHandler", "RequestHandler", "RpcEndpoint", "current_request_id"]

logger = logging.getLogger(__name__)

#: (method, params) → result。抛 ``RpcFault`` 即回错误响应。
RequestHandler = Callable[[str, Any], Awaitable[Any]]
#: (method, params) → None。
NotificationHandler = Callable[[str, Any], Awaitable[None]]

_current_request_id: contextvars.ContextVar[RequestId | None] = contextvars.ContextVar(
    "atlas_jsonrpc_request_id", default=None
)


def current_request_id() -> RequestId | None:
    """在请求处理函数内部调用：正在处理的请求的 id；不在请求处理中则为 None。

    处理函数的签名只有 (method, params)。个别场景需要 id（例如对方随后以 id 撤回
    这个请求），用它取，而不必为此改变所有处理函数的签名。
    """
    return _current_request_id.get()


class RpcEndpoint:
    def __init__(
        self,
        channel: MessageChannel,
        *,
        name: str,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
    ) -> None:
        self._channel = channel
        self._name = name
        self._on_request = on_request
        self._on_notification = on_notification
        self._ids = itertools.count(1)
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._handlers: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()

    # ------------------------------------------------------------------ 状态

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # ------------------------------------------------------------------ 发送

    async def request(self, method: str, params: Any, *, timeout: float | None) -> Any:
        """发请求并等待结果。

        对方回错误 → 抛 ``RpcFault``；超时 → 抛 ``TimeoutError``；通道关闭 → 抛 ``ChannelClosed``。
        ``timeout=None`` 表示一直等。超时后是否重启对方，由调用方决定，端点不做判断。
        """
        future = await self.start_request(method, params)
        return await asyncio.wait_for(future, timeout)

    async def start_request(self, method: str, params: Any) -> asyncio.Future[Any]:
        """发请求，立即返回一个 Future，不等待结果。

        用于可能持续很久的请求（ACP 的 ``session/prompt``）：调用方需要同时等待它、
        计时器与取消事件。Future 完成后自动从挂起表中移除；调用方取消 Future 即放弃等待。
        """
        if self.closed:
            raise ChannelClosed(f"{self._name}：通道已关闭")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        future.add_done_callback(lambda _f, rid=request_id: self._pending.pop(rid, None))
        try:
            await self._send(encode(Request(request_id, method, params)))
        except BaseException:
            future.cancel()
            raise
        return future

    async def notify(self, method: str, params: Any) -> None:
        await self._send(encode(Notification(method, params)))

    async def _send(self, text: str) -> None:
        if self.closed:
            raise ChannelClosed(f"{self._name}：通道已关闭")
        # 同一时刻只有一个发送在进行，保证每条消息完整地写出去，不与别的消息交错
        async with self._send_lock:
            await self._channel.send(text)

    # ------------------------------------------------------------------ 接收

    async def run(self) -> None:
        """读循环：直到通道关闭。"""
        try:
            while True:
                text = await self._channel.receive()
                if text is None:
                    break
                await self._dispatch(text)
        finally:
            await self._shutdown()

    async def _dispatch(self, text: str) -> None:
        try:
            frame = parse_frame(text)
        except InvalidFrame as exc:
            logger.warning("%s：收到非法帧：%s", self._name, exc)
            if exc.reply:
                await self._reply(Response(exc.request_id, error=exc.fault))
            return

        match frame:
            case Request():
                task = asyncio.create_task(
                    self._handle_request(frame), name=f"{self._name}:{frame.method}"
                )
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
            case Notification():
                await self._handle_notification(frame)
            case Response():
                self._resolve(frame)

    async def _handle_request(self, request: Request) -> None:
        if self._on_request is None:
            fault = RpcFault(ErrorCode.METHOD_NOT_FOUND, f"不支持的方法：{request.method}")
            await self._reply(Response(request.id, error=fault))
            return
        _current_request_id.set(request.id)  # 本任务独有的上下文，不影响其它请求
        try:
            result = await self._on_request(request.method, request.params)
        except RpcFault as fault:
            response = Response(request.id, error=fault)
        except asyncio.CancelledError:
            raise  # 端点正在关闭，不再回复
        except Exception:
            logger.exception("%s：处理请求 %s 时出错", self._name, request.method)
            response = Response(request.id, error=RpcFault(ErrorCode.INTERNAL_ERROR, "内部错误"))
        else:
            response = Response(request.id, result=result)
        await self._reply(response)

    async def _handle_notification(self, notification: Notification) -> None:
        if self._on_notification is None:
            return
        try:
            await self._on_notification(notification.method, notification.params)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 一条通知处理失败不能让整个读循环停下
            logger.exception("%s：处理通知 %s 时出错", self._name, notification.method)

    def _resolve(self, response: Response) -> None:
        future = self._pending.get(response.id) if response.id is not None else None
        if future is None or future.done():
            logger.warning("%s：收到无人等待的响应 id=%r", self._name, response.id)
            return
        if response.error is not None:
            future.set_exception(response.error)
        else:
            future.set_result(response.result)

    async def _reply(self, response: Response) -> None:
        try:
            await self._send(encode(response))
        except ChannelClosed:
            logger.debug("%s：通道已关闭，丢弃对 id=%r 的回复", self._name, response.id)

    async def _shutdown(self) -> None:
        self._closed.set()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ChannelClosed(f"{self._name}：通道已关闭"))
        self._pending.clear()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
