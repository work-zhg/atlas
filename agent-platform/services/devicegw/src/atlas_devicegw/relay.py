"""会话与文件转发（c4-model/agent/devicegw/code-devicegw-relay.html）。

    HostRelay      给 Agent 服务一个和 bridge 用法一样的会话地址：
                   取登记 → 找设备的连接 → 开会话流 → 对转
    FileRelay      读本机文件的请求变成文件流：首帧是响应头，之后是内容块
    ClosePolicy    关闭原因对照，只写这一处
    LaunchResolver 把登记里的启动参数变成连接器能直接用的（补上网关地址和会话令牌）

★ 只转发、不解析会话流：序号、确认、补发、审批都由 Agent 服务和 bridge 两端处理。
★ 设备不在本实例就关闭并说「可以重连」：Agent 服务重连前会重新查在线表，连到设备现在所在的实例；
  网关实例之间不转发。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from atlas_devicelink import (
    FILE,
    GOING_AWAY,
    INTERNAL_ERROR,
    NORMAL,
    SESSION,
    Channel,
    ChannelClosed,
)
from fastapi import WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from .capability import SessionRegistry
from .link import LinkServer, fit_reason

__all__ = ["ClosePolicy", "FileRelay", "HostRelay", "LaunchResolver"]

logger = logging.getLogger(__name__)

SUBPROTOCOL = "atlas.host.v1"
HEADER_SESSION = "x-session-id"
#: 会话已结束、被取代、本机拒绝：原样告诉 Agent 服务，它不再重连
SESSION_GONE = 4410
SUPERSEDED = 4409
DEVICE_REJECTED = 4403
#: 读文件时等首帧（响应头）的时限
FILE_HEAD_TIMEOUT_S = 30.0


class ClosePolicy:
    """关闭原因对照。设备那边断了、设备不在本实例、网关停机都说「可以重连」。"""

    PASS_THROUGH = frozenset({NORMAL, SESSION_GONE, SUPERSEDED, DEVICE_REJECTED})

    @classmethod
    def to_server(cls, code: int | None, reason: str) -> tuple[int, str]:
        if code in cls.PASS_THROUGH:
            assert code is not None
            return code, reason
        return GOING_AWAY, reason or "和设备的连接断了"


class LaunchResolver:
    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    def resolve(self, binding: dict[str, Any]) -> dict[str, Any]:
        """登记里的启动参数 → 开会话流时交给连接器的头。模型接入补上网关地址和会话令牌。"""
        launch = {k: v for k, v in binding["launch"].items() if k != "model"}
        launch["thread"] = binding["thread"]
        model = binding["launch"].get("model")
        if model:
            launch["model_env"] = {
                "base_url": self._registry.endpoint(model["upstream"]),
                "token": binding["token"],
                "model": model["model"],
                "small_model": model.get("small_model") or model["model"],
                "prompt_cache": bool(model.get("prompt_cache", True)),
                "headers": model.get("headers") or {},
            }
        return launch


class HostRelay:
    def __init__(
        self, links: LinkServer, registry: SessionRegistry, *, internal_token: str = ""
    ) -> None:
        self._links = links
        self._registry = registry
        self._resolver = LaunchResolver(registry)
        self._token = internal_token

    async def serve(self, ws: WebSocket, thread: str) -> None:
        if self._token and _bearer(ws) != self._token:
            await ws.close(4401)
            return
        if ws.headers.get(HEADER_SESSION) != thread:
            await ws.close(4403)
            return
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        binding = await self._registry.get(thread)
        if binding is None:
            await ws.close(INTERNAL_ERROR, "会话没有登记")
            return
        link = self._links.local(binding["device"])
        if link is None:
            await ws.close(GOING_AWAY, "设备不在本实例")
            return
        try:
            channel = await link.open_channel(SESSION, self._resolver.resolve(binding))
        except ConnectionError:
            await ws.close(GOING_AWAY, "和设备的连接断了")
            return
        await _pipe(ws, channel)


async def _pipe(ws: WebSocket, channel: Channel) -> None:
    """两边各一个任务对转，不解析内容；任何一端结束就按对照表关另一端。"""

    async def from_server() -> None:
        while True:
            try:
                msg = await ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if msg["type"] == "websocket.disconnect":
                return
            payload = msg.get("text")
            payload = payload if payload is not None else msg.get("bytes")
            if payload is None:
                continue
            try:
                await channel.send(payload)
            except ChannelClosed:
                return

    async def from_device() -> None:
        async for item in channel:
            if isinstance(item, str):
                await ws.send_text(item)
            else:
                await ws.send_bytes(item)

    server_task = asyncio.create_task(from_server())
    device_task = asyncio.create_task(from_device())
    try:
        done, _ = await asyncio.wait(
            {server_task, device_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (server_task, device_task):
            task.cancel()
        await asyncio.gather(server_task, device_task, return_exceptions=True)
    if server_task in done:
        # Agent 服务那边关了：断开不取消电脑上正在做的事，这一轮照常进行
        await channel.close(NORMAL, "Agent 服务断开")
        return
    code, reason = ClosePolicy.to_server(channel.close_code, channel.close_reason)
    with contextlib.suppress(Exception):
        await ws.close(code, fit_reason(reason))


class FileRelay:
    """读本机文件：每个请求一条文件流。op：changes / list / roots / fetch。"""

    def __init__(self, links: LinkServer) -> None:
        self._links = links

    async def request(self, device: str, body: dict[str, Any]) -> Response:
        link = self._links.local(device)
        if link is None:
            return _offline()
        try:
            channel = await link.open_channel(FILE, body)
            first = await asyncio.wait_for(channel.receive(), FILE_HEAD_TIMEOUT_S)
        except (ConnectionError, TimeoutError):
            return _offline()
        if first is None:
            if channel.close_code == GOING_AWAY:
                return _offline()
            return JSONResponse(
                {
                    "error": {
                        "kind": "device_error",
                        "message": channel.close_reason or "电脑没有回答",
                    }
                },
                status_code=502,
            )
        head = json.loads(first)
        status = int(head.get("status", 500))
        if body.get("op") != "fetch":
            await channel.close()
            return JSONResponse(head.get("body"), status_code=status)
        headers = {k: str(v) for k, v in (head.get("headers") or {}).items()}
        if status not in (200, 206):
            await channel.close()
            return Response(content=head.get("message", ""), status_code=status, headers=headers)
        return StreamingResponse(_chunks(channel), status_code=status, headers=headers)


async def _chunks(channel: Channel) -> AsyncIterator[bytes]:
    try:
        async for item in channel:
            yield item.encode() if isinstance(item, str) else item
    finally:
        await channel.close()


def _offline() -> JSONResponse:
    return JSONResponse(
        {"error": {"kind": "device_offline", "message": "电脑不在线"}}, status_code=503
    )


def _bearer(ws: WebSocket) -> str | None:
    scheme, _, value = ws.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
