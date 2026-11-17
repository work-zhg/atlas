"""TakeoverWindows：接管窗口。PopupKeeper：网页弹出的窗口挪回弹出它的那一块。

    open(id, url, screen)  记下此刻全部 cookie（接管前的样子），在这个对话那一块屏幕上开一个新窗口
                     打开 url、置前；同一个接管号再 open 只把窗口置前
    close(id, state) 关掉窗口。给了 state（沙箱：交还时要存登录态）就和接管前比较：变了的 cookie 的
                     域名，和浏览器此刻的登录态；没给（电脑上：登录就留在 profile 里）只关窗口
    SiteDiff         按（域名、名字、值）比：新增或变了的算；只删不增的（退出登录）不算
    PopupKeeper      常驻（windows keep）：window.open 的弹窗 Chromium 放在屏幕左上角（第 0 块），
                     挪到弹出它的窗口那一块

★ 专用窗口，不碰模型正在用的标签页：平台分不清哪个标签页属于哪个会话；登录写进的是整个浏览器
  共用的 cookie，模型的页面刷新一下就是登录状态。
★ 开窗口、关窗口、挪弹窗、交还时导出登录态都只用浏览器级的调试命令（rawcdp.WindowOps），不附着
  开着的页面：临时连一下 Playwright 也会给所有页面打开 Runtime，zhipin 这类网站当场就认出来
  （原先交还时经 Playwright 导出，会话窗口里的 zhipin 跳回空白，2026-11-17）。
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
from .rawcdp import CdpError, WindowOps
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
    def __init__(self, windows: WindowOps, paths: Paths) -> None:
        self._windows = windows
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
            if target and await self._windows.activate(target):
                return {"target": target, "reused": True}
        before = await self._windows.cookies()
        target = await self._windows.new_window(url, slot.bounds if slot else None)
        record.write_text(json.dumps({"target": target, "before": before}))
        os.chmod(record, 0o600)
        return {"target": target, "reused": False}

    async def close(self, takeover_id: str, state: CdpSession | None = None) -> dict[str, Any]:
        """state 给了返回 {domains, state}；没给返回 {closed}。

        没开过窗口（人直接交还）也给出此刻的登录态，domains 只比得出空。
        """
        record = self._record(takeover_id)
        data = json.loads(record.read_text()) if record.exists() else {}
        target = data.get("target")
        result: dict[str, Any] = {"closed": bool(target)}
        if state is not None:
            after = await state.cookies()
            # ★ 没有记录（没开过窗口）才拿此刻当「之前」；
            #   记录里的空列表是「接管前一个 cookie 都没有」
            before = data.get("before", after)
            origins = await state.page_origins()
            page = await self._windows.page(target) if target else None
            if page and (origin := origin_of(str(page.get("url") or ""))) is not None:
                origins.add(origin)
            result = {
                "domains": SiteDiff.changed(before, after),
                "state": {"cookies": after, "origins": await state.local_storage(origins)},
            }
        if target:
            await self._windows.close(target)
        record.unlink(missing_ok=True)
        return result


class PopupKeeper:
    """网页弹出的窗口挪回弹出它的那一块，否则人在自己的画面里看不到（第三方登录常这样弹窗）。

    ★ 只挪在自己新窗口里的、有 opener 的页面：网页里开的新标签页本来就在原窗口里。
    ★ 常驻，由守护看着：Chromium 重启后连接断了就退出，守护再拉起它。
    ★ 常驻的连接只「发现」新页面、不附着（原先经 Playwright 常连着，等于所有页面一直开着 Runtime）。
    """

    #: 弹窗刚建出来时窗口还没就位，等一下再量
    SETTLE_S = 0.3

    def __init__(self, windows: WindowOps) -> None:
        self._windows = windows
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> dict[str, Any]:
        await self._windows.on_target_created(self._created)
        await self._windows.disconnected()
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
            window, bounds = await self._windows.window_of(target)
            opener_window, opener_bounds = await self._windows.window_of(opener)
            if window == opener_window:
                return
            slot = Slot.at(int(opener_bounds["left"]))
            if Slot.at(int(bounds["left"])) == slot:
                return
            width = min(int(bounds.get("width") or SLOT_W), SLOT_W)
            height = min(int(bounds.get("height") or SLOT_H), SLOT_H)
            await self._windows.move_window(
                window, {"left": slot.left, "top": 0, "width": width, "height": height}
            )
        except (CdpError, KeyError, ValueError):  # 弹窗一闪就关了、opener 已经没了
            logger.debug("挪弹窗 %s 失败", target, exc_info=True)
