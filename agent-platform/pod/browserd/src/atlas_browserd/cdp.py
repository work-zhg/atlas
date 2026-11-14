"""CdpSession：经 Playwright 连到本机的 Chromium（connect_over_cdp），用它的默认上下文。

    cookies · add_cookies · clear_cookies     读写 cookie（整个浏览器共用一份）
    local_storage(origins)                    按源读本地存储：开着的页面直接读，
                                              没开的用一个临时页面读
    set_local_storage(origins)                按源写本地存储（临时页面）
    new_window(url) · close_window · activate · target_url   接管窗口（CDP 的 Target 域）
    page_origins()                            此刻开着的页面的源

★ 临时页面不联网：所有请求都由路由回一个空页面，只是为了站在那个源上读写它的本地存储
  （Playwright 自己导入 storageState 也是这么做的）。
★ 不调 browser.close()：连上的是一个已经在跑的浏览器，退出时只断开连接。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from .paths import CDP_PORT

__all__ = ["CdpSession", "connect", "origin_of"]

_BLANK = "<!doctype html><html><head><title></title></head><body></body></html>"


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = {"http": 80, "https": 443}[parts.scheme]
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


class CdpSession:
    def __init__(self, browser: Any) -> None:
        self._browser = browser
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Chromium 没有默认上下文")
        self._context = contexts[0]
        self._cdp: Any = None

    # ------------------------------------------------------------------ cookie

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(c) for c in await self._context.cookies()]

    async def add_cookies(self, cookies: Iterable[Mapping[str, Any]]) -> int:
        valid = [_cookie_input(c) for c in cookies]
        valid = [c for c in valid if c is not None]
        if valid:
            await self._context.add_cookies(valid)
        return len(valid)

    async def clear_cookies(self, domains: Iterable[str]) -> None:
        for domain in set(domains):
            await self._context.clear_cookies(domain=domain)

    # ------------------------------------------------------------------ 本地存储

    def page_origins(self) -> set[str]:
        return {o for p in self._context.pages if (o := origin_of(p.url)) is not None}

    async def local_storage(self, origins: Iterable[str]) -> list[dict[str, Any]]:
        out = []
        open_pages = {origin_of(p.url): p for p in self._context.pages}
        for origin in sorted(set(origins)):
            page = open_pages.get(origin)
            try:
                items = (
                    await _read_storage(page)
                    if page is not None
                    else await self._on_blank(origin, _read_storage)
                )
            except Exception:  # 页面正在跳转、源打不开：这个源这次没有
                continue
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

            async def write(page: Any, items: list[dict[str, str]] = items) -> None:
                script = "items => { for (const i of items) localStorage.setItem(i.name, i.value) }"
                await page.evaluate(script, items)

            try:
                await self._on_blank(origin, write)
            except Exception:
                continue
            written += 1
        return written

    async def clear_local_storage(self, origins: Iterable[str]) -> None:
        """开着的页面直接清，没开的在临时页面上清。

        ★ 不用 CDP 的 Storage.clearDataForOrigin：实测在浏览器级会话上不报错、也不生效。
        """

        async def clear(page: Any) -> None:
            await page.evaluate("() => localStorage.clear()")

        open_pages = {origin_of(p.url): p for p in self._context.pages}
        for origin in set(origins):
            page = open_pages.get(origin)
            try:
                await (clear(page) if page is not None else self._on_blank(origin, clear))
            except Exception:
                continue

    async def _on_blank(self, origin: str, work: Any) -> Any:
        """在一个不联网的临时页面上站到这个源，做完就关。"""
        page = await self._context.new_page()
        try:

            async def blank(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=_BLANK)

            await page.route("**/*", blank)
            await page.goto(origin + "/", wait_until="domcontentloaded", timeout=15_000)
            return await work(page)
        finally:
            await page.close()

    # ------------------------------------------------------------------ 窗口

    async def new_window(self, url: str) -> str:
        result = await self._send("Target.createTarget", {"url": url, "newWindow": True})
        target = str(result["targetId"])
        await self.activate(target)
        return target

    async def activate(self, target: str) -> bool:
        try:
            await self._send("Target.activateTarget", {"targetId": target})
        except Exception:
            return False
        return True

    async def close_window(self, target: str) -> None:
        try:
            await self._send("Target.closeTarget", {"targetId": target})
        except Exception:
            return

    async def target_url(self, target: str) -> str | None:
        result = await self._send("Target.getTargets", {})
        for info in result.get("targetInfos") or ():
            if info.get("targetId") == target:
                return str(info.get("url") or "")
        return None

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._cdp is None:
            self._cdp = await self._browser.new_browser_cdp_session()
        return await self._cdp.send(method, params)


async def _read_storage(page: Any) -> list[dict[str, str]]:
    entries = await page.evaluate("() => Object.entries(localStorage)")
    return [{"name": str(k), "value": str(v)} for k, v in entries]


def _cookie_input(cookie: Mapping[str, Any]) -> dict[str, Any] | None:
    """storageState 里的 cookie → add_cookies 收的形状；缺字段的丢掉。"""
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
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
        yield CdpSession(browser)
