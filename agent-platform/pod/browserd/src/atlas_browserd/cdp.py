"""CdpSession：登录态进出本机的 Chromium（默认上下文），只用浏览器级调试命令（rawcdp.WindowOps）。

    cookies · add_cookies · clear_cookies     读写 cookie（整个浏览器共用；Storage 域，不碰页面）
    local_storage(origins)                    按源读本地存储
    set_local_storage(origins)                按源写本地存储
    clear_local_storage(origins)              按源清本地存储
    page_origins()                            此刻开着的页面的源（只列，不附着）

★ 不附着任何开着的页面：原先经 Playwright 连上（connect_over_cdp），一连就给所有页面开 Runtime，
  交还接管时导出登录态，会话窗口里的 zhipin 当场认出自动化、跳回空白（2026-11-17）。
★ 本地存储在自己开的临时页面上读写，只附着这一个页面，做完就关：同一个 profile 里同源的本地存储
  是同一份，不用去碰开着的页面。临时页面不联网：Fetch 把所有请求都回成一个空页面，只为站在那个源上。
★ 不用 Storage.clearDataForOrigin：实测在浏览器级会话上不报错、也不生效。
★ cookie 的形状同 Playwright 的 storageState（Atlas 存的登录态一直是这个形状）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from .paths import CDP_PORT
from .rawcdp import CdpError, WindowOps, browser_session
from .screens import SLOT_W, SLOTS

__all__ = ["CdpSession", "connect", "origin_of"]

_BLANK = "<!doctype html><html><head><title></title></head><body></body></html>"
_BLANK_BODY = base64.b64encode(_BLANK.encode()).decode()
#: 临时页面开在虚拟屏幕最右边之外：不在任何对话的画面里闪一下
_OFFSCREEN = {"left": SLOTS * SLOT_W, "top": 0, "width": 800, "height": 600}
#: 一个源的读写（开页面、站到源上、求值）最多等多久
_ORIGIN_TIMEOUT_S = 15.0
#: storageState 里 cookie 的字段
_STATE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
#: Storage.setCookies 收的字段（CDP 的 CookieParam）；读出来的 cookie 写回去时只留这些
_PARAM_FIELDS = (
    *_STATE_FIELDS,
    "priority",
    "sameParty",
    "sourceScheme",
    "sourcePort",
    "partitionKey",
)


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = {"http": 80, "https": 443}[parts.scheme]
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


class CdpSession:
    def __init__(self, windows: WindowOps) -> None:
        self._windows = windows

    # ------------------------------------------------------------------ cookie

    async def cookies(self) -> list[dict[str, Any]]:
        return [_cookie_output(c) for c in await self._windows.cookies()]

    async def add_cookies(self, cookies: Iterable[Mapping[str, Any]]) -> int:
        valid = [c for c in map(_cookie_input, cookies) if c is not None]
        if valid:
            params = [_cookie_param(c) for c in valid]
            await self._windows.send("Storage.setCookies", {"cookies": params})
        return len(valid)

    async def clear_cookies(self, domains: Iterable[str]) -> None:
        """Storage 域不能按域名删：全清，再把别的域名的写回去（Playwright 也这样清）。"""
        drop = set(domains)
        current = await self._windows.cookies()
        keep = [c for c in current if c.get("domain") not in drop]
        if len(keep) == len(current):
            return
        await self._windows.send("Storage.clearCookies", {})
        if keep:
            params = [{k: c[k] for k in _PARAM_FIELDS if k in c} for c in keep]
            for param, cookie in zip(params, keep, strict=True):
                if cookie.get("session"):
                    param.pop("expires", None)
            await self._windows.send("Storage.setCookies", {"cookies": params})

    # ------------------------------------------------------------------ 本地存储

    async def page_origins(self) -> set[str]:
        return {o for p in await self._windows.pages() if (o := origin_of(str(p.get("url") or "")))}

    async def local_storage(self, origins: Iterable[str]) -> list[dict[str, Any]]:
        out = []
        for origin in sorted(set(origins)):
            try:
                entries = await self._on_blank(origin, "Object.entries(localStorage)")
            except (CdpError, TimeoutError):  # 源打不开：这个源这次没有
                continue
            items = [{"name": str(k), "value": str(v)} for k, v in entries or ()]
            if items:
                out.append({"origin": origin, "localStorage": items})
        return out

    async def set_local_storage(self, origins: Iterable[Mapping[str, Any]]) -> int:
        written = 0
        for entry in origins:
            origin = str(entry.get("origin") or "")
            items = [
                {"name": str(i.get("name")), "value": str(i.get("value"))}
                for i in entry.get("localStorage") or ()
                if isinstance(i, Mapping) and i.get("name") is not None
            ]
            if not origin_of(origin) or not items:
                continue
            script = (
                "(items => { for (const i of items) localStorage.setItem(i.name, i.value) })"
                f"({json.dumps(items, ensure_ascii=False)})"
            )
            try:
                await self._on_blank(origin, script)
            except (CdpError, TimeoutError):
                continue
            written += 1
        return written

    async def clear_local_storage(self, origins: Iterable[str]) -> None:
        for origin in set(origins):
            with contextlib.suppress(CdpError, TimeoutError):
                await self._on_blank(origin, "localStorage.clear()")

    async def _on_blank(self, origin: str, expression: str) -> Any:
        """在一个不联网的临时页面上站到这个源，求 expression 的值，做完就关。"""
        want = origin_of(origin)
        if want is None:
            raise CdpError(f"不是网页的源：{origin}")
        params = {"url": "about:blank", "newWindow": True, "background": True, **_OFFSCREEN}
        target = str((await self._windows.send("Target.createTarget", params))["targetId"])
        try:
            return await asyncio.wait_for(
                self._evaluate_at(target, want, expression), _ORIGIN_TIMEOUT_S
            )
        finally:
            await self._windows.close(target)

    async def _evaluate_at(self, target: str, origin: str, expression: str) -> Any:
        windows = self._windows
        attached = await windows.send(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )
        session = str(attached["sessionId"])
        replies: set[asyncio.Task[Any]] = set()

        def blank(params: dict[str, Any]) -> None:
            reply = {
                "requestId": params["requestId"],
                "responseCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                "body": _BLANK_BODY,
            }
            task = asyncio.get_running_loop().create_task(
                _quietly(windows.send("Fetch.fulfillRequest", reply, session=session))
            )
            replies.add(task)
            task.add_done_callback(replies.discard)

        windows.on("Fetch.requestPaused", blank, session=session)
        try:
            await windows.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=session)
            await windows.send("Page.navigate", {"url": origin + "/"}, session=session)
            # 导航提交前，页面还是 about:blank（源是 "null"）
            while await self._evaluate(session, "location.origin") != origin:
                await asyncio.sleep(0.05)
            return await self._evaluate(session, expression)
        finally:
            windows.off("Fetch.requestPaused", session=session)

    async def _evaluate(self, session: str, expression: str) -> Any:
        """Runtime.evaluate 不用先 Runtime.enable。"""
        result = await self._windows.send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}, session=session
        )
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise CdpError(str(detail.get("exception", {}).get("description") or detail))
        return result.get("result", {}).get("value")


async def _quietly(work: Any) -> None:
    with contextlib.suppress(CdpError):
        await work


def _cookie_output(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """CDP 的 cookie → storageState 的形状（Playwright 导出时也是这样：没写 sameSite 补 Lax）。"""
    out = {k: cookie[k] for k in _STATE_FIELDS if k in cookie}
    out.setdefault("sameSite", "Lax")
    if cookie.get("session"):
        out["expires"] = -1
    return out


def _cookie_param(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """_cookie_input 的结果 → Storage.setCookies 的 CookieParam：会话 cookie 不带 expires。"""
    out = dict(cookie)
    if out.get("expires", -1) <= 0:
        out.pop("expires", None)
    return out


def _cookie_input(cookie: Mapping[str, Any]) -> dict[str, Any] | None:
    """storageState 里的 cookie → 规整后的形状；缺字段的丢掉。"""
    name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
    if not name or value is None or not domain:
        return None
    out: dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "domain": str(domain),
        "path": str(cookie.get("path") or "/"),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
    }
    try:
        expires = float(cookie.get("expires", -1))
    except (TypeError, ValueError):
        expires = -1
    out["expires"] = expires if expires > 0 else -1
    same_site = cookie.get("sameSite")
    if same_site in ("Strict", "Lax", "None"):
        out["sameSite"] = same_site
    return out


@asynccontextmanager
async def connect(port: int = CDP_PORT) -> AsyncIterator[CdpSession]:
    async with browser_session(port) as windows:
        yield CdpSession(windows)
