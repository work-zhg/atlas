"""一个主体（Key）能用什么，以及按主体的短时缓存。

★ 判权结果按主体缓存：每次工具调用都过网关，不能每个请求都问用户中心。新授、收回的授权在缓存
  时长内生效（本实例改了授权立即清掉）。用户中心不可用、缓存又过期时一律拒绝（Unavailable），
  不放行。
★ 控制面（替 Atlas 回答「这把 Key 能用什么」）和数据面（每个请求判权）共用一份，看到的一致。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["AccessCache", "KeyAccess"]


@dataclass(frozen=True)
class KeyAccess:
    """一个主体被授了哪些私有服务、哪些工具。

    servers  服务级授权：这些服务的全部工具（含以后新增的）
    tools    工具级授权：按服务分组（用户中心的数据标识「服务名/工具名」只在防腐层解析一次）
    """

    servers: frozenset[str] = frozenset()
    tools: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def allows(self, service: str, tool: str) -> bool:
        return service in self.servers or tool in self.tools.get(service, ())

    def tools_of(self, service: str) -> frozenset[str]:
        return self.tools.get(service, frozenset())

    def touches(self, service: str) -> bool:
        return service in self.servers or bool(self.tools.get(service))


class _Source(Protocol):
    async def key_access(self, subject: str) -> KeyAccess: ...


class AccessCache:
    """「主体 → KeyAccess」。用户中心不可用时：没过期的照用，过期了 Unavailable 往上抛。"""

    def __init__(self, source: _Source, ttl_s: float) -> None:
        self._source = source
        self._ttl = ttl_s
        self._rows: dict[str, tuple[float, KeyAccess]] = {}

    async def get(self, subject: str) -> KeyAccess:
        row = self._rows.get(subject)
        if row is not None and row[0] >= time.monotonic():
            return row[1]
        found = await self._source.key_access(subject)
        if self._ttl > 0:
            if len(self._rows) > 50_000:
                self._rows.clear()
            self._rows[subject] = (time.monotonic() + self._ttl, found)
        return found

    def forget(self, subject: str | None = None) -> None:
        """本进程里刚改过授权：立即生效（别的实例等缓存过期）。"""
        if subject is None:
            self._rows.clear()
        else:
            self._rows.pop(subject, None)
