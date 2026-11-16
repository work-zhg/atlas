"""浏览器 · 设备宿主（c4-model/agent/browser/code-browser-device.html）：本机 CLI 智能体的浏览器在
使用者自己的电脑上。

    DeviceBrowserHost   确保（只核对）、开关接管窗口、剪贴板、状态、画面路线：都经设备网关请连接器做
    DeviceBrowser       一台电脑上的浏览器：哪台设备、连接器上线时的报告
    DeviceViewRoute     画面怎么走：设备所在网关实例上的画面流
                        （只带会话和对话，没有地址、端口、口令）
    DeviceUnavailable   用不了的原因 → 给人看的话（不说「系统忙」）

★ 平台够不到电脑：每个动作都是请连接器做一件固定的事。拉起浏览器是连接器自己的事，ensure 只核对
  电脑在线、装了浏览器组件、本机允许查看画面。
★ 没有附加件：登录态留在电脑的 profile 里，不导入、不导出，交还时也不比较、不存（saves_logins）。
★ 画面不要口令：画面服务只听电脑本机的 Unix 套接字，谁能看由这里核对本人、设备网关核对设备。
★ 每次都核对：会话绑定的电脑是会话创建人的、没被吊销（同设备会话 DeviceSessions）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..device.gateway import DeviceGateway, DeviceStatus, GatewayUnavailable
from ..repositories.device import DeviceRepository
from .host import BrowserUnavailable, screen_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..db.models import Device, Thread

__all__ = [
    "DeviceBrowser",
    "DeviceBrowserHost",
    "DeviceBrowserUnavailable",
    "DeviceUnavailable",
    "DeviceViewRoute",
    "on_device",
]

logger = logging.getLogger(__name__)


def on_device(thread: Thread) -> bool:
    """这个会话跑在使用者自己的电脑上（本机 CLI 智能体）：浏览器也在那台电脑上。"""
    return thread.device_uuid is not None


class DeviceUnavailable(Enum):
    OFFLINE = "你的电脑「{name}」不在线：在电脑上启动连接器（atlas-connect run）后再试"
    NOT_INSTALLED = (
        "电脑「{name}」没装浏览器组件：在电脑上运行 atlas-connect browser install，装好后重启连接器"
    )
    REMOTE_VIEW_OFF = (
        "电脑「{name}」不允许远程查看浏览器画面：电脑主人在本机策略里关掉了它"
        "（~/.atlas/connect.toml 的 [browser] remote_view）"
    )
    REVOKED = "这个会话绑定的电脑「{name}」已被吊销：请新建一个会话"
    NOT_OWNER = "这个会话绑定的电脑不是你的"

    def message(self, name: str = "") -> str:
        return self.value.format(name=name)


class DeviceBrowserUnavailable(BrowserUnavailable):
    """电脑上的浏览器用不了：消息给人看，reason 给画面挑关闭原因。"""

    def __init__(self, reason: DeviceUnavailable, name: str = "") -> None:
        super().__init__(reason.message(name))
        self.reason = reason


@dataclass(frozen=True)
class DeviceBrowser:
    device: Device
    status: DeviceStatus

    @property
    def name(self) -> str:
        return self.device.name


@dataclass(frozen=True)
class DeviceViewRoute:
    url: str
    token: str


class DeviceBrowserHost:
    """BrowserHost 的第三个实现（按请求构造：拿请求的数据库会话查设备）。"""

    kind = "device"
    #: 登录态留在电脑上：交还时不比较、不存
    saves_logins = False

    def __init__(self, session: AsyncSession, gateway: DeviceGateway) -> None:
        self._session = session
        self._gateway = gateway

    # ------------------------------------------------------------------ 确保

    async def ensure(self, thread: Thread, *, view: bool = True) -> DeviceBrowser:
        """只核对：电脑在线、装了浏览器组件、（要看画面时）本机允许查看画面。不满足抛
        BrowserUnavailable（消息给人看）。"""
        device = await self._device(thread)
        try:
            status = await self._gateway.presence(str(device.device_uuid))
        except GatewayUnavailable as exc:
            logger.warning("查设备 %s 在不在线时设备网关不可用：%s", device.device_uuid, exc)
            raise
        if status is None:
            raise DeviceBrowserUnavailable(DeviceUnavailable.OFFLINE, device.name)
        browser = status.browser
        if not browser.get("installed"):
            raise DeviceBrowserUnavailable(DeviceUnavailable.NOT_INSTALLED, device.name)
        if view and browser.get("remote_view") is False:
            raise DeviceBrowserUnavailable(DeviceUnavailable.REMOTE_VIEW_OFF, device.name)
        return DeviceBrowser(device, status)

    async def _device(self, thread: Thread) -> Device:
        device = await DeviceRepository(self._session).get(thread.device_uuid)  # type: ignore[arg-type]
        if device is None or device.owner_uuid != thread.created_by_uuid:
            raise DeviceBrowserUnavailable(DeviceUnavailable.NOT_OWNER)
        if device.status != "active":
            raise DeviceBrowserUnavailable(DeviceUnavailable.REVOKED, device.name)
        return device

    # ------------------------------------------------------------------ 接管窗口

    async def open_takeover(self, thread: Thread, takeover_id: str, url: str) -> dict[str, Any]:
        """在对话那一块开接管窗口（浏览器没在跑由连接器先拉起，可能要几十秒）。"""
        browser = await self.ensure(thread)
        return await self._ask(
            browser,
            {
                "op": "takeover.open",
                "thread": str(thread.thread_uuid),
                "screen": screen_of(thread),
                "takeover": takeover_id,
                "url": url,
            },
        )

    async def close_takeover(self, thread: Thread, takeover_id: str) -> None:
        """只关窗口：登录就留在电脑的 profile 里。尽力而为（电脑不在线就算了）。"""
        browser = await self.ensure(thread, view=False)
        await self._ask(
            browser,
            {"op": "takeover.close", "thread": str(thread.thread_uuid), "takeover": takeover_id},
        )

    # ------------------------------------------------------------------ 状态、剪贴板、画面

    async def running(self, thread: Thread) -> bool:
        """这个对话在电脑上的浏览器里有没有画面（浏览器在跑、对话用过浏览器）。不抛错。"""
        try:
            browser = await self.ensure(thread, view=False)
            answer = await self._ask(browser, {"op": "status", "screen": screen_of(thread)})
        except (BrowserUnavailable, GatewayUnavailable):
            return False
        return bool(answer.get("running")) and answer.get("slot") is not None

    async def clipboard_set(self, thread: Thread, text: str) -> None:
        await self._ask(await self.ensure(thread), {"op": "clipboard.set", "text": text})

    async def clipboard_get(self, thread: Thread) -> str:
        answer = await self._ask(await self.ensure(thread), {"op": "clipboard.get"})
        return str(answer.get("text") or "")

    async def view_route(self, thread: Thread) -> DeviceViewRoute:
        browser = await self.ensure(thread)
        url, token = self._gateway.view_url(
            browser.status, str(thread.thread_uuid), screen_of(thread)
        )
        return DeviceViewRoute(url, token)

    async def _ask(self, browser: DeviceBrowser, body: dict[str, Any]) -> dict[str, Any]:
        status, answer = await self._gateway.browser(browser.status, body)
        if status == 503:
            raise DeviceBrowserUnavailable(DeviceUnavailable.OFFLINE, browser.name)
        if status != 200:
            raise BrowserUnavailable(_message_of(answer) or f"电脑上的浏览器出错了（{status}）")
        return answer if isinstance(answer, dict) else {}


def _message_of(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return ""
