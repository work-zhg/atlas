"""UpstreamConnection：一条上游连接的收发与分派（代码设计 §7.9）。

接收循环里：
  · 请求（session.* / turn.*） → 各自一个任务交给 dispatch，响应经 Outbox 排队发出
  · session.ack 通知 → Outbox.ack
  · 响应（server 对 permission.ask 的答复） → HostSession.on_permission_answer

不用 RpcEndpoint：bridge 发出的消息都由 Outbox 预先编好 seq 与 id，这条连接只负责收发。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from atlas_host import AckParams, AttachParams, CloseCode, methods
from atlas_jsonrpc import (
    ChannelClosed,
    ErrorCode,
    InvalidFrame,
    Notification,
    Request,
    Response,
    RpcFault,
    encode,
    parse_frame,
)
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from ..errors import BridgeError
from ..session.host import HostSession
from .dispatch import dispatch, to_fault
from .outbox import Outbox

__all__ = ["UpstreamConnection"]

logger = logging.getLogger(__name__)


class UpstreamConnection:
    def __init__(self, ws: ServerConnection, host: HostSession, outbox: Outbox) -> None:
        self._ws = ws
        self._host = host
        self._outbox = outbox
        self._tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()

    @property
    def remote(self) -> Any:
        return self._ws.remote_address

    # ------------------------------------------------------------------ 发送（供 Outbox 调用）

    async def send_text(self, text: str) -> None:
        async with self._send_lock:
            try:
                await self._ws.send(text)
            except ConnectionClosed as exc:
                raise ChannelClosed("上游连接已关闭") from exc

    async def close(self, code: int, reason: str = "") -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._ws.close(code, reason)

    async def close_after_drain(self, code: int, reason: str = "") -> None:
        """等此前放入 Outbox 的消息（包括响应）发完再关闭。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._outbox.drained(), 5)
        await self.close(code, reason)

    # ------------------------------------------------------------------ 接收

    async def run(self) -> None:
        # ★ 断开时不取消进行中的请求：session.open、turn.start 改变的是会话状态，
        #   与连接无关；它们照常完成，响应随这条连接一起丢弃（Outbox 只发给原连接）
        with contextlib.suppress(ConnectionClosed):
            async for message in self._ws:
                self._on_message(message)

    def _on_message(self, message: str | bytes) -> None:
        try:
            frame = parse_frame(message)
        except InvalidFrame as exc:
            logger.warning("上游发来不合法的消息：%s", exc)
            if exc.reply:
                self._respond(Response(exc.request_id, error=exc.fault))
            return
        match frame:
            case Request(method=methods.SESSION_ATTACH):
                self._handle_attach(frame)
            case Request():
                self._outbox.release(self)  # 第一个请求：开始向这条连接发送
                self._spawn(self._handle_request(frame))
            case Notification(method=methods.SESSION_ACK, params=params):
                self._handle_ack(params)
            case Notification(method=method):
                logger.info("忽略上游通知 %s", method)
            case Response():
                self._handle_response(frame)

    async def _handle_request(self, request: Request) -> None:
        try:
            result = await dispatch(self._host, request.method, request.params)
            response = Response(request.id, result=result)
        except RpcFault as fault:
            response = Response(request.id, error=fault)
        except Exception:
            logger.exception("处理上游请求 %s 时出错", request.method)
            response = Response(request.id, error=RpcFault(ErrorCode.INTERNAL_ERROR, "内部错误"))
        self._respond(response)
        if request.method == methods.SESSION_CLOSE and response.ok:
            # 会话已结束：响应发出后以 4410 关闭，告诉 server 不要重连（§4.5）
            await self.close_after_drain(CloseCode.SESSION_GONE, "session closed")

    def _handle_attach(self, request: Request) -> None:
        """session.attach：算出补发计划、放入响应、重排发送队列 —— 在同一个同步段里完成，
        中间没有新消息插进来，于是「响应之后按 seq 补发，再接实时消息」严格成立（§7.3）。"""
        try:
            params = AttachParams.model_validate(request.params)
            result = self._host.attach(params)
        except ValidationError as exc:
            fault = RpcFault(ErrorCode.INVALID_PARAMS, "参数不合法", {"cause": "invalid_params"})
            self._outbox.release(self)
            self._respond(Response(request.id, error=fault))
            logger.warning("session.attach 参数不合法：%s", exc)
            return
        except BridgeError as exc:
            self._outbox.release(self)
            self._respond(Response(request.id, error=to_fault(methods.SESSION_ATTACH, exc)))
            return
        self._outbox.rewind(self, encode(Response(request.id, result=result.to_wire())))

    def _handle_ack(self, params: Any) -> None:
        try:
            self._outbox.ack(AckParams.model_validate(params).seq)
        except ValidationError:
            logger.warning("session.ack 参数不合法：%r", params)

    def _handle_response(self, response: Response) -> None:
        if not isinstance(response.id, int):
            logger.warning("收到 id 不认识的响应：%r", response.id)
            return
        error = response.error.to_json() if response.error is not None else None
        self._host.on_permission_answer(response.id, response.result, error)

    def _respond(self, response: Response) -> None:
        self._outbox.put_response(self, encode(response))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
