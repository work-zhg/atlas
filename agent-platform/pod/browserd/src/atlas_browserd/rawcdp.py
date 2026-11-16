"""WindowOps：只管窗口的浏览器级调试命令（c4-model/agent/browser/code-browser-screen.html）。

    browser_session()   连到 Chromium 的浏览器级调试端点（/json/version 给的地址）
    WindowOps           开窗口（放在对话那一块）、找窗口、挪窗口、置前、关窗口、列页面、
                        读 cookie（接管前记下来比较）、新页面的事件（挪弹窗用）

★ 只发 Target、Browser、Storage 域的命令，不附着页面、不开 Runtime：Playwright 一连上就给所有页面
  开 Runtime，会检测自动化的网站（如 zhipin）据此认出「有人在遥控」，把页面跳成 about:blank。
  会话窗口、接管窗口、挪弹窗都走这里，computer use 才是干净的。
★ 连接本机回环，不走代理（电脑上常配着 HTTP_PROXY）。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from .paths import CDP_PORT

__all__ = ["CdpError", "WindowOps", "browser_session"]


class CdpError(RuntimeError):
    """Chromium 回了错误，或连接断了。"""


class WindowOps:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._closed = asyncio.Event()
        self._reader = asyncio.get_running_loop().create_task(self._read())

    # ------------------------------------------------------------------ 窗口

    async def new_window(self, url: str, bounds: Mapping[str, int] | None = None) -> str:
        """开一个新窗口；给了 bounds 就直接开在那里（屏幕的某一块，不先在别处闪一下）。"""
        params: dict[str, Any] = {"url": url, "newWindow": True, **(bounds or {})}
        target = str((await self.send("Target.createTarget", params))["targetId"])
        await self.activate(target)
        return target

    async def window_of(self, target: str) -> tuple[int, dict[str, Any]]:
        """target 所在的窗口：(windowId, 位置和大小)。"""
        found = await self.send("Browser.getWindowForTarget", {"targetId": target})
        return int(found["windowId"]), dict(found["bounds"])

    async def move_window(self, window_id: int, bounds: Mapping[str, int]) -> None:
        # ★ 最大化、全屏、最小化的窗口不能直接改位置：先回到普通状态
        await self.send(
            "Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}}
        )
        await self.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": dict(bounds)})

    async def move_target(self, target: str, bounds: Mapping[str, int]) -> None:
        window, _ = await self.window_of(target)
        await self.move_window(window, bounds)

    async def activate(self, target: str) -> bool:
        try:
            await self.send("Target.activateTarget", {"targetId": target})
        except CdpError:
            return False
        return True

    async def close(self, target: str) -> None:
        with contextlib.suppress(CdpError):
            await self.send("Target.closeTarget", {"targetId": target})

    async def pages(self) -> list[dict[str, Any]]:
        """此刻的页面（targetInfo：targetId、url、title、openerId……）。"""
        infos = (await self.send("Target.getTargets", {})).get("targetInfos") or ()
        return [dict(i) for i in infos if i.get("type") == "page"]

    async def page(self, target: str) -> dict[str, Any] | None:
        return next((p for p in await self.pages() if p.get("targetId") == target), None)

    # ------------------------------------------------------------------ cookie

    async def cookies(self) -> list[dict[str, Any]]:
        """整个浏览器（默认上下文）的 cookie。浏览器级命令，不碰页面。"""
        return [dict(c) for c in (await self.send("Storage.getCookies", {})).get("cookies") or ()]

    # ------------------------------------------------------------------ 事件

    async def on_target_created(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """新开的 target（标签页、弹窗……）都告诉 handler（参数是 CDP 的 targetInfo）。

        ★ 只是「发现」，不附着：页面里什么都不会多出来。
        """
        self._handlers.setdefault("Target.targetCreated", []).append(
            lambda params: handler(params["targetInfo"])
        )
        await self.send("Target.setDiscoverTargets", {"discover": True})

    async def disconnected(self) -> None:
        """等到和 Chromium 的连接断开（Chromium 退出或重启）。"""
        await self._closed.wait()

    # ------------------------------------------------------------------ 收发

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._closed.is_set():
            raise CdpError("和 Chromium 的调试连接断了")
        msg_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            message = {"id": msg_id, "method": method, "params": params or {}}
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            raise CdpError(f"{method} 发不出去：{exc}") from exc
        return await future

    async def aclose(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    future = self._pending.pop(int(msg["id"]), None)
                    if future is None or future.done():
                        continue
                    if "error" in msg:
                        error = msg["error"]
                        future.set_exception(CdpError(str(error.get("message") or error)))
                    else:
                        future.set_result(dict(msg.get("result") or {}))
                    continue
                for handler in self._handlers.get(str(msg.get("method")), ()):
                    handler(dict(msg.get("params") or {}))
        except Exception:  # 连接断了：等着的命令都失败
            pass
        finally:
            self._closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("和 Chromium 的调试连接断了"))
            self._pending.clear()


@asynccontextmanager
async def browser_session(port: int = CDP_PORT) -> AsyncIterator[WindowOps]:
    from websockets.asyncio.client import connect

    from .supervisor import cdp_version

    version = await asyncio.to_thread(cdp_version, port, 3.0)
    if not version or not version.get("webSocketDebuggerUrl"):
        raise CdpError("连不上 Chromium 的调试端口：浏览器没在运行")
    ws = await connect(
        str(version["webSocketDebuggerUrl"]), max_size=None, proxy=None, open_timeout=10
    )
    ops = WindowOps(ws)
    try:
        yield ops
    finally:
        await ops.aclose()
