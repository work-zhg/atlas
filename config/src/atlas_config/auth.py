"""认证（设计 §14；权限设计 doc/auth-permission-design.html §9）。

两条通道、两种凭据，互不通用：

  /config/*    用户（浏览器直连）。
               dev 模式：读 X-User-Id，没给就用默认用户，操作码全有（与运行时同规则）。
               uc 模式：取浏览器带来的 Atlas 登录会话（Cookie atlas_session），交给
               atlas-server 的 /internal/v1/auth/introspect 校验，拿回用户 id 与操作码；
               写请求校验 CSRF 双提交。本服务不维护登录、不接用户中心。
  /internal/*  运行时进程。共享令牌 ATLAS_CONFIG_INTERNAL_TOKEN；**不接受用户身份**
               —— 内部接口能读到全部已发布内容，不能让浏览器直接打到它。

★ 与运行时各自实现，不抽公共包：规则只有几十行，抽出来就是第三个所有服务都
  依赖的包，收益抵不上耦合。
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, Request

from .settings import ConfigSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["Principal", "current_principal", "require_any", "require_internal"]

SESSION_COOKIE = "atlas_session"
CSRF_COOKIE = "atlas_csrf"
CSRF_HEADER = "x-atlas-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}

#: dev 模式的全部操作码（与 atlas-server auth/catalog.py 的目录一致）
DEV_OPERATIONS = frozenset(
    {
        "chat:use",
        "agent:create",
        "agent:manage_all",
        "skill:upload",
        "skill:review",
        "mcp:manage",
        "mcp:manage_all",
        "model:manage",
        "service_account:manage",
        "audit:view",
    }
)


@dataclass(frozen=True)
class Principal:
    user: str
    operations: frozenset[str] = field(default_factory=frozenset)

    def has_any(self, *ops: str) -> bool:
        return any(op in self.operations for op in ops)


class _IntrospectCache:
    """会话令牌 → (过期时刻, 主体)。只缓存成功结果；失败每次都问。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, Principal]] = {}

    def get(self, token: str) -> Principal | None:
        row = self._rows.get(token)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(token, None)
            return None
        return row[1]

    def put(self, token: str, p: Principal, ttl: float) -> None:
        if len(self._rows) > 10_000:  # 防止无界增长
            self._rows.clear()
        self._rows[token] = (time.monotonic() + ttl, p)


_cache = _IntrospectCache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _introspect(settings: ConfigSettings, token: str) -> Principal:
    hit = _cache.get(token)
    if hit is not None:
        return hit
    internal = settings.internal_token.get_secret_value() if settings.internal_token else ""
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.post(
                f"{settings.server_url.rstrip('/')}/internal/v1/auth/introspect",
                json={"token": token},
                headers={"X-Internal-Token": internal},
            )
    except httpx.HTTPError as exc:
        logger.warning("会话校验失败（atlas-server 不可达）：%s", exc)
        raise HTTPException(status_code=503, detail="暂时无法校验登录，请稍后重试") from exc
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="请先登录")
    if r.status_code != 200:
        logger.warning("会话校验返回 %s", r.status_code)
        raise HTTPException(status_code=503, detail="暂时无法校验登录，请稍后重试")
    body = r.json()
    p = Principal(user=str(body["user_id"]), operations=frozenset(body.get("operations") or []))
    _cache.put(token, p, settings.introspect_ttl_s)
    return p


async def current_principal(
    request: Request,
    settings: Annotated[ConfigSettings, Depends(get_settings)],
    x_user_id: Annotated[str | None, Header()] = None,
) -> Principal:
    if settings.auth_mode == "uc":
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="请先登录")
        if request.method not in _SAFE:
            cookie = request.cookies.get(CSRF_COOKIE)
            if not cookie or cookie != request.headers.get(CSRF_HEADER):
                raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面后重试")
        return await _introspect(settings, token)
    user = (x_user_id or "").strip() or settings.default_user
    return Principal(user=user, operations=DEV_OPERATIONS)


def require_any(*ops: str):  # type: ignore[no-untyped-def]
    """有其中任一操作码即可（前端隐藏只负责「看不到」）。"""

    def dep(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.has_any(*ops):
            raise HTTPException(status_code=403, detail="没有权限执行该操作")
        return principal

    return dep


def require_internal(
    settings: Annotated[ConfigSettings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.internal_token.get_secret_value() if settings.internal_token else ""
    if not expected:
        return  # 本机开发：启动时已告警（__main__）
    given = (authorization or "").removeprefix("Bearer ").strip()
    # ★ 定时比较：令牌比对不能因前缀匹配长度而泄露信息
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="内部接口需要服务间令牌")
