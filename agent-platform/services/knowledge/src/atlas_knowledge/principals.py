"""调用者 → 主体标签（doc/knowledge-enterprise-design.html §7.4 文档级权限）。

    u:用户  d:部门（含各级上级）  r:角色  g:用户组
文档的 acl 与它有交集（或文档为 public）才能被检索到。

uc 模式问 atlas-server（/internal/v1/auth/principals，它从用户中心展开部门树与角色），结果在
进程内缓存一小段时间。展开失败时只用 u:用户（只会少看到，不会多看到）。
内网免令牌：带本次调用的身份请求头（auth.outbound_headers）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

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
    ttl = settings.principals_ttl_s
    url = f"{settings.server_url.rstrip('/')}/internal/v1/auth/principals"

    async def resolve(user_id: str) -> list[str]:
        hit = cache.get(user_id)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        try:
            async with httpx.AsyncClient(
                timeout=5.0, trust_env=False, transport=transport
            ) as client:
                r = await client.post(url, json={"user_id": user_id}, headers=outbound_headers())
            r.raise_for_status()
            tags = [str(t) for t in r.json().get("principals") or []] or [f"u:{user_id}"]
        except httpx.HTTPError as exc:
            logger.warning("主体展开失败，只按用户本人检索：%s", exc)
            return [f"u:{user_id}"]
        if len(cache) > 10_000:
            cache.clear()
        cache[user_id] = (time.monotonic() + ttl, tags)
        return tags

    return resolve
