"""StateIO：登录态进出 Chromium（形状同 Playwright 的 storageState）。

    import_(bundle)            cookie 直接写进浏览器；本地存储按源写进去
    export(sites, origins)     这些站点的 cookie，和这些站点的源（开着的页面 + Atlas 给的）
                               的本地存储
    forget(site)               删掉这个站点的 cookie 和本地存储（尽力而为）

    SiteMatch                  域名 / 源属不属于某个站点：站点本身或它的子域名

★ 站点（可注册域名）由 Atlas 按公共后缀表算好交进来，这里只做后缀匹配。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .cdp import CdpSession

__all__ = ["SiteMatch", "StateIO"]


class SiteMatch:
    def __init__(self, sites: Iterable[str]) -> None:
        self._sites = {s.strip().lower().lstrip(".") for s in sites if s.strip()}

    def domain(self, domain: str) -> bool:
        host = domain.strip().lower().lstrip(".").rstrip(".")
        return any(host == s or host.endswith("." + s) for s in self._sites)

    def origin(self, origin: str) -> bool:
        host = urlsplit(origin).hostname
        return bool(host) and self.domain(host)  # type: ignore[arg-type]

    def common_origins(self) -> set[str]:
        """清站点时没有开着的页面：至少把站点本身和 www 两个源清掉。"""
        return {f"https://{p}{s}" for s in self._sites for p in ("", "www.")}


class StateIO:
    def __init__(self, cdp: CdpSession) -> None:
        self._cdp = cdp

    async def import_(self, bundle: Mapping[str, Any]) -> dict[str, int]:
        cookies = await self._cdp.add_cookies(bundle.get("cookies") or ())
        origins = await self._cdp.set_local_storage(bundle.get("origins") or ())
        return {"cookies": cookies, "origins": origins}

    async def export(self, sites: Iterable[str], origins: Iterable[str] = ()) -> dict[str, Any]:
        match = SiteMatch(sites)
        cookies = [c for c in await self._cdp.cookies() if match.domain(str(c.get("domain", "")))]
        wanted = {o for o in (*await self._cdp.page_origins(), *origins) if match.origin(o)}
        return {"cookies": cookies, "origins": await self._cdp.local_storage(wanted)}

    async def forget(self, site: str) -> dict[str, int]:
        match = SiteMatch([site])
        domains = {
            str(c["domain"]) for c in await self._cdp.cookies() if match.domain(str(c["domain"]))
        }
        await self._cdp.clear_cookies(domains)
        origins = {o for o in await self._cdp.page_origins() if match.origin(o)}
        origins |= match.common_origins()
        await self._cdp.clear_local_storage(origins)
        return {"domains": len(domains)}
