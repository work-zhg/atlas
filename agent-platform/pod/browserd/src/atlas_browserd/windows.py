"""TakeoverWindows：接管窗口。PopupKeeper：网页弹出的窗口挪回弹出它的那一块。

    open(id, url, screen)  记下此刻全部 cookie（接管前的样子），在这个对话那一块屏幕上开一个新窗口
                     打开 url、置前；同一个接管号再 open 只把窗口置前
    close(id)        关掉窗口，和接管前比较：变了的 cookie 的域名，和浏览器此刻的登录态
    SiteDiff         按（域名、名字、值）比：新增或变了的算；只删不增的（退出登录）不算
    PopupKeeper      常驻（windows keep）：window.open 的弹窗 Chromium 放在屏幕左上角（第 0 块），
                     挪到弹出它的窗口那一块

★ 专用窗口，不碰模型正在用的标签页：平台分不清哪个标签页属于哪个会话；登录写进的是整个浏览器
  共用的 cookie，模型的页面刷新一下就是登录状态。
★ 接管记录（窗口的 target、接管前的 cookie）只放本地盘，关窗口时删掉。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

from .cdp import CdpSession, origin_of
from .paths import Paths
from .screens import SLOT_H, SLOT_W, ScreenTable, Slot

__all__ = ["PopupKeeper", "SiteDiff", "TakeoverWindows"]

logger = logging.getLogger("atlas_browserd")


class SiteDiff:
    @staticmethod
    def key(cookie: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(cookie.get("domain", "")).lower().lstrip("."),
            str(cookie.get("name", "")),
            str(cookie.get("value", "")),
        )

    @classmethod
    def changed(
        cls, before: Iterable[Mapping[str, Any]], after: Iterable[Mapping[str, Any]]
    ) -> list[str]:
        seen = {cls.key(c) for c in before}
        return sorted({k[0] for c in after if (k := cls.key(c)) not in seen and k[0]})


class TakeoverWindows:
    def __init__(self, cdp: CdpSession, paths: Paths) -> None:
        self._cdp = cdp
        self._paths = paths

    def _record(self, takeover_id: str) -> Any:
        safe = "".join(ch for ch in takeover_id if ch.isalnum() or ch == "-")
        if not safe:
            raise ValueError("接管号不合法")
        return self._paths.takeovers / f"{safe}.json"

    async def open(self, takeover_id: str, url: str, screen: str | None = None) -> dict[str, Any]:
        """screen = 对话（根会话 id）：窗口开在这个对话那一块，人看这个对话的画面就是它。"""
        slot = ScreenTable(self._paths).acquire(screen) if screen else None
        record = self._record(takeover_id)
        if record.exists():
            target = json.loads(record.read_text()).get("target")
            if target and await self._cdp.activate(target):
                return {"target": target, "reused": True}
        before = await self._cdp.cookies()
        target = await self._cdp.new_window(url, slot.bounds if slot else None)
        record.write_text(json.dumps({"target": target, "before": before}))
        os.chmod(record, 0o600)
        return {"target": target, "reused": False}

    async def close(self, takeover_id: str) -> dict[str, Any]:
        """返回 {domains, state}。

        没开过窗口（人直接交还）也给出此刻的登录态，domains 只比得出空。
        """
        record = self._record(takeover_id)
        data = json.loads(record.read_text()) if record.exists() else {}
        target = data.get("target")
        after = await self._cdp.cookies()
        # ★ 没有记录（没开过窗口）才拿此刻当「之前」；记录里的空列表是「接管前一个 cookie 都没有」
        before = data.get("before", after)
        domains = SiteDiff.changed(before, after)
        origins = set(self._cdp.page_origins())
        url = await self._cdp.target_url(target) if target else None
        if url and (origin := origin_of(url)) is not None:
            origins.add(origin)
        state = {"cookies": after, "origins": await self._cdp.local_storage(origins)}
        if target:
            await self._cdp.close_window(target)
        record.unlink(missing_ok=True)
        return {"domains": domains, "state": state}


class PopupKeeper:
    """网页弹出的窗口挪回弹出它的那一块，否则人在自己的画面里看不到（第三方登录常这样弹窗）。

    ★ 只挪在自己新窗口里的、有 opener 的页面：网页里开的新标签页本来就在原窗口里。
    ★ 常驻，由守护看着：Chromium 重启后连接断了就退出，守护再拉起它。
    """

    #: 弹窗刚建出来时窗口还没就位，等一下再量
    SETTLE_S = 0.3

    def __init__(self, cdp: CdpSession) -> None:
        self._cdp = cdp
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> dict[str, Any]:
        await self._cdp.on_target_created(self._created)
        await self._cdp.disconnected()
        return {"stopped": "Chromium 断开了"}

    def _created(self, info: dict[str, Any]) -> None:
        if info.get("type") != "page" or not info.get("openerId"):
            return
        rehome = self.rehome(info["targetId"], info["openerId"])
        task = asyncio.get_running_loop().create_task(rehome)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def rehome(self, target: str, opener: str) -> None:
        await asyncio.sleep(self.SETTLE_S)
        try:
            window, bounds = await self._cdp.window_of(target)
            opener_window, opener_bounds = await self._cdp.window_of(opener)
            if window == opener_window:
                return
            slot = Slot.at(int(opener_bounds["left"]))
            if Slot.at(int(bounds["left"])) == slot:
                return
            width = min(int(bounds.get("width") or SLOT_W), SLOT_W)
            height = min(int(bounds.get("height") or SLOT_H), SLOT_H)
            await self._cdp.move_window(
                window, {"left": slot.left, "top": 0, "width": width, "height": height}
            )
        except Exception:  # 弹窗一闪就关了、opener 已经没了
            logger.debug("挪弹窗 %s 失败", target, exc_info=True)
