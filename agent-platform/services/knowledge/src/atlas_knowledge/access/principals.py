"""调用者 → 主体标签（doc/knowledge-enterprise-design.html §7.4 文档级权限）。

    u:用户  d:部门（含各级上级）  r:角色
文档的 acl 与它有交集（或文档为 public）才能被检索到。

经 UserCenterGateway 问用户中心：角色（授权快照）、所在部门（用户）、
各级上级（部门树，按租户缓存）。
结果在进程内缓存一小段时间。展开失败时只用 u:用户（只会少看到，不会多看到），不缓存。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from ..errors import KnowledgeError
from .usercenter import UserCenterGateway

__all__ = ["PrincipalResolver", "PrincipalTags"]

logger = logging.getLogger(__name__)

PrincipalResolver = Callable[[str], Awaitable[list[str]]]


class PrincipalTags:
    def __init__(self, gateway: UserCenterGateway, *, ttl_s: float = 60.0) -> None:
        self._uc = gateway
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, list[str]]] = {}
        #: 租户 id → (过期时刻, 部门 id → 上级部门 id)
        self._trees: dict[str, tuple[float, dict[str, str | None]]] = {}

    async def _parents(self, tenant: str) -> dict[str, str | None]:
        hit = self._trees.get(tenant)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        tree = {
            str(d["id"]): (str(d["parent_id"]) if d.get("parent_id") else None)
            for d in await self._uc.dept_tree()
        }
        self._trees[tenant] = (time.monotonic() + self._ttl, tree)
        return tree

    async def resolve(self, user_id: str) -> list[str]:
        hit = self._cache.get(user_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        tags = [f"u:{user_id}"]
        try:
            tags += [f"r:{r}" for r in await self._uc.roles(user_id)]
            user = await self._uc.user(user_id)
            dept = str((user.get("dept") or {}).get("id") or "") or None
            if dept:
                tree = await self._parents(str(user.get("tenant_id") or ""))
                seen: set[str] = set()
                while dept and dept not in seen:
                    seen.add(dept)
                    tags.append(f"d:{dept}")
                    dept = tree.get(dept)
        except KnowledgeError as exc:
            logger.warning("主体展开失败，只按用户本人检索：%s", exc.message)
            return [f"u:{user_id}"]
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[user_id] = (time.monotonic() + self._ttl, tags)
        return tags
