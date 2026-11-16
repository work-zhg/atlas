"""浏览器 · 画面（c4-model/agent/browser/code-browser-view.html）：沙箱里的虚拟屏幕实时转到控制台。

    CloseReason     关掉连接的原因 → WebSocket 关闭码和给人看的话
    ViewTarget      要连的画面：哪个沙箱、哪个端口、屏幕的哪一块、带什么鉴权头
    ViewAccess      谁能看、看哪个：来的人必须是会话的创建人，这个会话的浏览器必须正在跑；
                    再问守护这个对话在屏幕的第几块（屏幕按对话分块，各看各的）
    ViewRelay       两边原样转发：不解析画面协议、不落日志（画面里有密码输入）
    ViewKeepAlive   连着画面时每 60 秒给沙箱记一次活动（人在里面慢慢登录也不会被闲置回收）
    LiveView        一条画面连接从头到尾
    DeviceView      电脑上的浏览器（本机 CLI 智能体）：经设备网关的画面流，按宿主选路
                    （code-browser-device.html · DeviceViewRoute）

★ 控制台里的 noVNC 随控制台发布，经同源 WebSocket 连到这里；沙箱只出一条画面数据流。
★ 不为看画面去建沙箱：要建是接管的「打开」做的事。
★ 连接本身不存任何状态，落在哪个实例上都一样（画面口令由 sandbox_uuid 派生）。
★ 电脑上的画面不经沙箱：连到设备所在网关实例的画面流，连接器再接到那一块的画面服务。连着画面
  算浏览器在用（连接器那边记），不用这边保活。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ..db.models import Thread
from ..db.session import fetch_one
from ..sandbox.base.execution.channels import ChannelUnavailable, SandboxChannels
from ..sandbox.base.lifecycle.runtime import SandboxGone
from .daemon import DaemonClient, DaemonError, ViewKey
from .host import BrowserHosts, screen_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CloseReason",
    "DeviceView",
    "LiveView",
    "ViewAccess",
    "ViewDenied",
    "ViewKeepAlive",
    "ViewTarget",
]

logger = logging.getLogger(__name__)

#: websockify 的 WebSocket 路径
VIEW_PATH = "/websockify"


class CloseReason(Enum):
    """(WebSocket 关闭码, 给人看的话)。★ 关闭原因最多 123 字节，中文一个字 3 字节：话要短。"""

    NOT_OWNER = (4403, "只有会话的创建人能看浏览器画面")
    NO_BROWSER = (4404, "浏览器没在运行")
    NO_SCREEN = (4404, "这个对话还没用过浏览器")
    SANDBOX_GONE = (4410, "沙箱已经不在了")
    UPSTREAM_CLOSED = (4502, "浏览器画面断开了")
    DEVICE_OFFLINE = (4503, "你的电脑不在线")
    DEVICE_REFUSED = (4403, "这台电脑不允许远程查看浏览器画面")

    @property
    def code(self) -> int:
        return self.value[0]

    @property
    def text(self) -> str:
        return self.value[1]


class ViewDenied(Exception):
    def __init__(self, reason: CloseReason) -> None:
        super().__init__(reason.text)
        self.reason = reason


@dataclass(frozen=True)
class ViewTarget:
    sandbox_uuid: UUID
    port: int
    headers: dict[str, str]
    #: 屏幕的哪一块：websockify 按 token 转到那块的画面服务
    token: str

    @property
    def path(self) -> str:
        return f"{VIEW_PATH}?token={self.token}"


class ViewAccess:
    """会话的创建人才能看（浏览器和登录态都是他的）；浏览器必须正在跑；只看这个对话那一块。"""

    def __init__(self, hosts: BrowserHosts, keys: ViewKey, port: int, daemon: DaemonClient) -> None:
        self._hosts = hosts
        self._keys = keys
        self._port = port
        self._daemon = daemon

    async def check(self, session: AsyncSession, user: UUID, thread_uuid: UUID) -> ViewTarget:
        thread = await fetch_one(session, Thread, Thread.thread_uuid == thread_uuid)
        if thread is None or thread.created_by_uuid != user:
            raise ViewDenied(CloseReason.NOT_OWNER)
        ref = await self._hosts.current(thread)
        if ref is None:
            raise ViewDenied(CloseReason.NO_BROWSER)
        try:
            token = await self._daemon.screen_token(ref, screen_of(thread))
        except DaemonError as exc:
            raise ViewDenied(CloseReason.NO_BROWSER) from exc
        if token is None:
            raise ViewDenied(CloseReason.NO_SCREEN)
        return ViewTarget(
            ref.sandbox_uuid, self._port, self._keys.auth_header(ref.sandbox_uuid), token
        )


class ViewKeepAlive:
    """连着画面时每 interval 秒记一次活动。只推迟闲置回收：人关了画面，沙箱照常按时限回收。"""

    def __init__(self, channels: SandboxChannels, *, interval_s: float = 60.0) -> None:
        self._channels = channels
        self._interval_s = interval_s

    async def run(self, sandbox_uuid: UUID) -> None:
        while True:
            try:
                await self._channels.keep_alive(sandbox_uuid)
            except Exception:
                logger.warning("给沙箱 %s 记活动失败", sandbox_uuid, exc_info=True)
            await asyncio.sleep(self._interval_s)


class ViewRelay:
    """两边原样转发二进制帧，任一端断开就收尾。"""

    async def pipe(self, client: WebSocket, upstream: Any) -> CloseReason | None:
        """返回 None = 客户端自己断开（不用再给它关闭原因）。"""

        async def down() -> CloseReason:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await client.send_bytes(frame)
                else:
                    await client.send_text(frame)
            return CloseReason.UPSTREAM_CLOSED

        async def up() -> None:
            while True:
                message = await client.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if (data := message.get("bytes")) is not None:
                    await upstream.send(data)
                elif (text := message.get("text")) is not None:
                    await upstream.send(text)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        first = done.pop()
        try:
            return first.result()
        except (WebSocketDisconnect, ConnectionError):
            return None
        except Exception:
            return CloseReason.UPSTREAM_CLOSED


class LiveView:
    """一条画面连接从头到尾：核对 → 打开画面通道 → 转发 + 保活 → 收尾。"""

    def __init__(
        self,
        access: ViewAccess,
        channels: SandboxChannels,
        *,
        keep_alive: ViewKeepAlive | None = None,
    ) -> None:
        self._access = access
        self._channels = channels
        self._alive = keep_alive or ViewKeepAlive(channels)
        self._relay = ViewRelay()

    async def check(self, session: AsyncSession, user: UUID, thread_uuid: UUID) -> ViewTarget:
        """核对（用短会话，连接开始前就关掉）。不通过抛 ViewDenied。"""
        return await self._access.check(session, user, thread_uuid)

    async def connect(
        self, client: WebSocket, target: ViewTarget, *, subprotocols: Sequence[str] = ()
    ) -> None:
        """核对通过之后：连上沙箱的画面服务，原样转发直到任一端断开。"""
        try:
            async with self._channels.open_ws(
                target.sandbox_uuid,
                target.port,
                path=target.path,
                headers=target.headers,
                subprotocols=subprotocols,
            ) as upstream:
                await client.accept(subprotocol=upstream.subprotocol)
                alive = asyncio.create_task(self._alive.run(target.sandbox_uuid))
                try:
                    reason = await self._relay.pipe(client, upstream)
                finally:
                    alive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await alive
        except SandboxGone:
            await close(client, CloseReason.SANDBOX_GONE)
            return
        except ChannelUnavailable:
            logger.info("连不上沙箱 %s 的画面服务", target.sandbox_uuid, exc_info=True)
            await close(client, CloseReason.NO_BROWSER)
            return
        if reason is not None:
            await close(client, reason)


class DeviceView:
    """电脑上的浏览器画面：连设备网关的画面流，原样转发。核对本人、找设备由调用方先做完
    （DeviceBrowserHost.view_route）。"""

    def __init__(self, relay: ViewRelay | None = None) -> None:
        self._relay = relay or ViewRelay()

    async def connect(
        self, client: WebSocket, route: Any, *, subprotocols: Sequence[str] = ()
    ) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake

        headers = {"Authorization": f"Bearer {route.token}"} if route.token else {}
        try:
            async with connect(
                route.url,
                additional_headers=headers,
                # ★ 设备网关在集群内：不走代理
                proxy=None,
                compression=None,
                max_size=None,
                open_timeout=15,
            ) as upstream:
                # noVNC 要 binary 子协议（沙箱那边由 websockify 应答，这里替它应答）
                await client.accept(subprotocol="binary" if "binary" in subprotocols else None)
                reason = await self._relay.pipe(client, upstream)
                if reason is CloseReason.UPSTREAM_CLOSED:
                    reason = device_close(upstream.close_code, upstream.close_reason or "")
        except (OSError, InvalidHandshake, TimeoutError):
            logger.info("连不上设备网关的画面流：%s", route.url, exc_info=True)
            await close(client, CloseReason.DEVICE_OFFLINE)
            return
        if reason is not None:
            await close(client, reason)


def device_close(code: int | None, reason: str) -> CloseReason:
    """设备网关、连接器关画面流的原因 → 给人看的。"""
    if code == 4403:
        return CloseReason.DEVICE_REFUSED
    if code == 4404:
        return CloseReason.NO_BROWSER if "没在运行" in reason else CloseReason.NO_SCREEN
    if code == 1001:
        return CloseReason.DEVICE_OFFLINE
    return CloseReason.UPSTREAM_CLOSED


async def close(client: WebSocket, reason: CloseReason) -> None:
    """带着原因关掉（还没握手的先接受，客户端才读得到原因）。"""
    with contextlib.suppress(Exception):
        if client.application_state is WebSocketState.CONNECTING:
            await client.accept()
        if client.application_state is WebSocketState.CONNECTED:
            await client.close(code=reason.code, reason=reason.text)
