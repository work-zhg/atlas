"""HostClient：server 到 bridge 的一条上游连接（Bridge 设计 §4.3；代码设计 §11）。

基于 atlas_jsonrpc.RpcEndpoint。server 方向不需要补发：它发出的都是短请求，失败重试即可，
turnId 保证幂等（§4.1）。bridge 发来的通知与请求（permission.ask）交给回调。

★ 连接与会话生命周期的管理（常驻连接、session.attach、租约、重连退避）在下一步；
  目前一轮一条连接，由 HostRuntime 管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from atlas_host import HEADER_SESSION, SUBPROTOCOL_V1
from atlas_jsonrpc import ChannelClosed, NotificationHandler, RequestHandler, RpcEndpoint
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

__all__ = ["HostClient", "HostUnreachable"]

logger = logging.getLogger(__name__)

#: 与 bridge 的单条消息上限一致（Bridge 设计 §4.10）
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class HostUnreachable(RuntimeError):
    """连不上 bridge（握手失败、网络不通、鉴权被拒）。"""


class _WsChannel:
    """把 websockets 的连接适配成 MessageChannel。"""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws

    async def send(self, text: str) -> None:
        try:
            await self._ws.send(text)
        except ConnectionClosed as exc:
            raise ChannelClosed("上游连接已关闭") from exc

    async def receive(self) -> str | None:
        try:
            message = await self._ws.recv()
        except ConnectionClosed:
            return None
        return message if isinstance(message, str) else message.decode()


class HostClient:
    """用作 async context manager。"""

    def __init__(
        self,
        url: str,
        *,
        token: str,
        session_id: str,
        on_notification: NotificationHandler,
        on_request: RequestHandler,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._token = token
        self._session_id = session_id
        self._on_notification = on_notification
        self._on_request = on_request
        self._connect_timeout_s = connect_timeout_s
        self._ws: ClientConnection | None = None
        self._endpoint: RpcEndpoint | None = None
        self._reader: asyncio.Task[None] | None = None

    async def __aenter__(self) -> HostClient:
        try:
            async with asyncio.timeout(self._connect_timeout_s):
                self._ws = await connect(
                    self._url,
                    additional_headers={
                        "Authorization": f"Bearer {self._token}",
                        HEADER_SESSION: self._session_id,
                    },
                    subprotocols=[SUBPROTOCOL_V1],  # type: ignore[list-item]
                    compression=None,
                    max_size=MAX_MESSAGE_BYTES,
                    ping_interval=20,
                    ping_timeout=20,
                    # ★ 绝不走代理：对端永远是会话 Pod。原因见 acp/channel.py 的同名注释
                    #   （macOS 的系统代理设置会把 127.0.0.1 也拐进 SOCKS）。
                    proxy=None,
                )
        except Exception as exc:
            msg = f"连不上 bridge（{self._url}）：{exc}"
            raise HostUnreachable(msg) from exc
        self._endpoint = RpcEndpoint(
            _WsChannel(self._ws),
            name="bridge",
            on_request=self._on_request,
            on_notification=self._on_notification,
        )
        self._reader = asyncio.create_task(self._endpoint.run(), name="host-client-read")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._reader, 5)

    # ------------------------------------------------------------------ 收发

    @property
    def closed(self) -> bool:
        return self._endpoint is None or self._endpoint.closed

    @property
    def close_code(self) -> int | None:
        return self._ws.close_code if self._ws is not None else None

    async def wait_closed(self) -> None:
        if self._reader is not None:
            await asyncio.shield(self._reader)

    async def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        """失败抛 RpcFault（bridge 回了错误）、ChannelClosed、TimeoutError。"""
        assert self._endpoint is not None, "先进入 async with"
        return await self._endpoint.request(method, params, timeout=timeout)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._endpoint is not None, "先进入 async with"
        await self._endpoint.notify(method, params)
