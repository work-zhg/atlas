"""TakeoverWindows：接管窗口。

    open(id, url)    记下此刻全部 cookie（接管前的样子），开一个新窗口打开 url、置前；
                     同一个接管号再 open 只把窗口置前
    close(id)        关掉窗口，和接管前比较：变了的 cookie 的域名，和浏览器此刻的登录态
    SiteDiff         按（域名、名字、值）比：新增或变了的算；只删不增的（退出登录）不算

★ 专用窗口，不碰模型正在用的标签页：平台分不清哪个标签页属于哪个会话；登录写进的是整个浏览器
  共用的 cookie，模型的页面刷新一下就是登录状态。
★ 接管记录（窗口的 target、接管前的 cookie）只放本地盘，关窗口时删掉。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any

from .cdp import CdpSession, origin_of
from .paths import Paths

__all__ = ["SiteDiff", "TakeoverWindows"]


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

    async def open(self, takeover_id: str, url: str) -> dict[str, Any]:
        record = self._record(takeover_id)
        if record.exists():
            target = json.loads(record.read_text()).get("target")
            if target and await self._cdp.activate(target):
                return {"target": target, "reused": True}
        before = await self._cdp.cookies()
        target = await self._cdp.new_window(url)
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
