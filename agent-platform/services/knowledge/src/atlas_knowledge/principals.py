"""调用者 → 主体标签（doc/knowledge-enterprise-design.html §7.4 文档级权限）。

    u:用户  d:部门（含各级上级）  r:角色
文档的 acl 与它有交集（或文档为 public）才能被检索到。

直接问用户中心开放接口（以 Atlas 应用的身份，同 perms.py）：
    /open/v1/users/{id}/authz   r:  Atlas 应用内的角色
    /open/v1/users/{id}         d:  所在部门
    /open/v1/depts/tree         d:  各级上级（按租户缓存）
结果在进程内缓存一小段时间。展开失败时只用 u:用户（只会少看到，不会多看到），不缓存。
内网免令牌：带本次调用的身份请求头（auth.outbound_headers）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .auth import outbound_headers
from .settings import KnowledgeSettings

__all__ = ["PrincipalResolver", "make_resolver"]

logger = logging.getLogger(__name__)

PrincipalResolver = Callable[[str], Awaitable[list[str]]]


def make_resolver(
    settings: KnowledgeSettings, transport: httpx.AsyncBaseTransport | None = None
) -> PrincipalResolver:
    cache: dict[str, tuple[float, list[str]]] = {}
    #: 租户 id → (过期时刻, 部门 id → 上级部门 id)
    trees: dict[str, tuple[float, dict[str, str | None]]] = {}
    ttl = settings.principals_ttl_s

    async def get(client: httpx.AsyncClient, path: str) -> Any:
        r = await client.get(path)
        r.raise_for_status()
        return r.json()

    async def parents(client: httpx.AsyncClient, tenant: str) -> dict[str, str | None]:
        hit = trees.get(tenant)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        tree = {
            str(d["id"]): (str(d["parent_id"]) if d.get("parent_id") else None)
            for d in await get(client, "/open/v1/depts/tree")
        }
        trees[tenant] = (time.monotonic() + ttl, tree)
        return tree

    async def resolve(user_id: str) -> list[str]:
        hit = cache.get(user_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        tags = [f"u:{user_id}"]
        try:
            async with httpx.AsyncClient(
                base_url=settings.uc_base_url.rstrip("/"),
                headers=outbound_headers(),
                timeout=5.0,
                trust_env=False,
                transport=transport,
            ) as client:
                snap = await get(client, f"/open/v1/users/{user_id}/authz")
                tags += [f"r:{r}" for r in snap.get("roles") or []]
                user = await get(client, f"/open/v1/users/{user_id}")
                dept = str((user.get("dept") or {}).get("id") or "") or None
                if dept:
                    tree = await parents(client, str(user.get("tenant_id") or ""))
                    seen: set[str] = set()
                    while dept and dept not in seen:
                        seen.add(dept)
                        tags.append(f"d:{dept}")
                        dept = tree.get(dept)
        except httpx.HTTPError as exc:
            logger.warning("主体展开失败，只按用户本人检索：%s", exc)
            return [f"u:{user_id}"]
        if len(cache) > 10_000:
            cache.clear()
        cache[user_id] = (time.monotonic() + ttl, tags)
        return tags

    return resolve
