"""数据权限（§3.7）：打开开关的作用域 = 用户中心里的一个数据编码，挂在本服务的应用名下；
作用域下的每个实例是这个编码下的一条数据，数据标识用实例的 uuid。

    只读  列出主题目录、加载主题、浏览条目与修订
    读写  写入、新建 / 修改主题、拆分、移动、撤销、恢复
    Owner 彻底删除（单条，或整个实例）
    登记  不校验：第一次登记的 Owner 生效，之后的登记不改动

★ 由本服务代为登记：用户中心只允许数据编码的所属应用登记和查询授权，客户端调用本服务的
  「登记实例归属」，本服务去用户中心写第一个 Owner。已有授权的数据用户中心只允许 Owner 修改
  （OWNER_REQUIRED）—— 这正好就是「已登记，直接返回成功」。
★ 用户中心不可用时一律拒绝（Unavailable），不放行。
★ 只缓存「有权限」的结果：刚登记的实例如果先被查过一次「没有权限」，缓存会让它在 TTL 内一直被拒。

两个实现：UCDataPermissions（用户中心）；测试里的内存实现（tests/conftest.py）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import httpx

from .auth import outbound_headers
from .errors import Unavailable

logger = logging.getLogger(__name__)

__all__ = ["LEVEL_RANK", "CachedPermissions", "DataPermissions", "UCDataPermissions"]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


class DataPermissions(Protocol):
    async def level(self, user_id: str, data_code: str, data_id: str) -> str:
        """有效级别 NONE | READ | WRITE | OWNER（含用户中心的编码级授权）。"""
        ...

    async def accessible(
        self, user_id: str, data_code: str, min_level: str = "READ"
    ) -> tuple[set[str], str]:
        """→ (级别够的数据标识, 编码级授权的级别)。编码级 ≥ min_level 时该编码下全部可访问。"""
        ...

    async def register(self, data_code: str, data_id: str, data_name: str, owner: str) -> bool:
        """写第一个 Owner。返回 True = 这次写入的；False = 早已有授权（不改动）。"""
        ...

    async def cleanup(self, data_code: str, data_id: str) -> None:
        """实例被彻底删除：清掉这条数据的全部授权。"""
        ...


class UCDataPermissions:
    """用户中心开放接口（/open/v1/data-permissions*）。"""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10.0, trust_env=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            logger.warning("用户中心不可达：%s", exc)
            raise Unavailable("用户中心暂不可用，暂时无法校验数据权限") from exc
        return r

    @staticmethod
    def _code(r: httpx.Response) -> str:
        try:
            return str(r.json().get("code") or "")
        except ValueError:
            return ""

    def _fail(self, r: httpx.Response) -> Unavailable:
        logger.warning("用户中心拒绝了开放接口调用：%s %s", r.status_code, r.text[:200])
        return Unavailable("用户中心暂不可用，暂时无法校验数据权限")

    async def level(self, user_id: str, data_code: str, data_id: str) -> str:
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": data_code, "data_id": data_id, "user_id": user_id},
        )
        if r.status_code == 404:
            # 用户中心里没有这个用户（或编码没登记）：没有任何权限
            return "NONE"
        if r.status_code != 200:
            raise self._fail(r)
        return str(r.json().get("permission") or "NONE")

    async def accessible(
        self, user_id: str, data_code: str, min_level: str = "READ"
    ) -> tuple[set[str], str]:
        out: set[str] = set()
        page, type_level = 1, "NONE"
        while True:
            r = await self._call(
                "GET",
                "/open/v1/data-permissions/accessible",
                params={
                    "data_code": data_code,
                    "user_id": user_id,
                    "min": min_level,
                    "page": page,
                    "size": 500,
                },
            )
            if r.status_code == 404:
                return set(), "NONE"
            if r.status_code != 200:
                raise self._fail(r)
            body = r.json()
            type_level = str(body.get("type_permission") or "NONE")
            items = body.get("items") or []
            out.update(str(i["data_id"]) for i in items)
            if len(out) >= int(body.get("total") or 0) or not items:
                return out, type_level
            page += 1

    async def register(self, data_code: str, data_id: str, data_name: str, owner: str) -> bool:
        r = await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": data_code,
                "data_id": data_id,
                "data_name": data_name[:100],
                "grants": [{"permission": "OWNER", "subject_type": "USER", "subject_id": owner}],
            },
        )
        if r.status_code < 300:
            return True
        if self._code(r) == "OWNER_REQUIRED":
            return False  # 已有授权：第一次登记的 Owner 生效
        raise self._fail(r)

    async def cleanup(self, data_code: str, data_id: str) -> None:
        r = await self._call(
            "DELETE",
            "/open/v1/data-permissions",
            params={"data_code": data_code, "data_id": data_id},
        )
        if r.status_code >= 300 and r.status_code != 404:
            raise self._fail(r)


class CachedPermissions:
    """level() 结果的短时缓存（风险表「每次读写多一次权限查询」）。只缓存级别 > NONE 的结果。"""

    def __init__(self, inner: DataPermissions, ttl_s: float) -> None:
        self.inner = inner
        self._ttl = ttl_s
        self._rows: dict[tuple[str, str, str], tuple[float, str]] = {}

    async def level(self, user_id: str, data_code: str, data_id: str) -> str:
        key = (user_id, data_code, data_id)
        row = self._rows.get(key)
        if row is not None and row[0] >= time.monotonic():
            return row[1]
        level = await self.inner.level(user_id, data_code, data_id)
        if level != "NONE" and self._ttl > 0:
            if len(self._rows) > 50_000:
                self._rows.clear()
            self._rows[key] = (time.monotonic() + self._ttl, level)
        return level

    async def accessible(
        self, user_id: str, data_code: str, min_level: str = "READ"
    ) -> tuple[set[str], str]:
        return await self.inner.accessible(user_id, data_code, min_level)

    async def register(self, data_code: str, data_id: str, data_name: str, owner: str) -> bool:
        return await self.inner.register(data_code, data_id, data_name, owner)

    async def cleanup(self, data_code: str, data_id: str) -> None:
        await self.inner.cleanup(data_code, data_id)
        for key in [k for k in self._rows if k[1] == data_code and k[2] == data_id]:
            self._rows.pop(key, None)
