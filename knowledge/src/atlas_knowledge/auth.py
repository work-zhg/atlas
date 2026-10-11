"""认证（权限设计 doc/auth-permission-design.html §9；知识库设计 §11）。

三种调用方：
  浏览器       uc 模式：Cookie atlas_session 交给 atlas-server introspect；写请求校验 CSRF 双提交。
               dev 模式：X-User-Id（缺省 default_user），操作码全有。
  服务账号     Authorization: Bearer atlas_sk_…，同样交给 atlas-server introspect。
  atlas-server /internal/*：X-Internal-Token（运行时按智能体绑定检索，权限在运行时那边判）。

★ 与 atlas-config 各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal

import httpx
from fastapi import Depends, Header, HTTPException, Request

from .settings import KnowledgeSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "DEV_OPERATIONS",
    "Principal",
    "current_principal",
    "require_internal",
]

SESSION_COOKIE = "atlas_session"
CSRF_COOKIE = "atlas_csrf"
CSRF_HEADER = "x-atlas-csrf"
SERVICE_KEY_PREFIX = "atlas_sk_"
_SAFE = {"GET", "HEAD", "OPTIONS"}

#: 本服务关心的操作码（在 atlas-server auth/catalog.py 登记到用户中心）
KB_OPERATIONS = frozenset({"kb:create", "kb:manage_all", "kb:review"})
#: dev 模式的操作码
DEV_OPERATIONS = KB_OPERATIONS | {"chat:use", "agent:create"}


@dataclass(frozen=True)
class Principal:
    #: 用户 id 或服务账号 id（字符串形式的 uuid）
    id: str
    kind: Literal["user", "service"] = "user"
    name: str = ""
    operations: frozenset[str] = field(default_factory=frozenset)

    def has(self, op: str) -> bool:
        return op in self.operations


class _Cache:
    """凭据 → (过期时刻, 主体)。只缓存成功结果。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, Principal]] = {}

    def get(self, key: str) -> Principal | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: str, p: Principal, ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, p)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _introspect(settings: KnowledgeSettings, body: dict[str, str]) -> Principal:
    key = "|".join(f"{k}={v}" for k, v in sorted(body.items()))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    internal = settings.internal_token.get_secret_value() if settings.internal_token else ""
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.post(
                f"{settings.server_url.rstrip('/')}/internal/v1/auth/introspect",
                json=body,
                headers={"X-Internal-Token": internal},
            )
    except httpx.HTTPError as exc:
        logger.warning("凭据校验失败（atlas-server 不可达）：%s", exc)
        raise HTTPException(status_code=503, detail="暂时无法校验登录，请稍后重试") from exc
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="请先登录")
    if r.status_code != 200:
        logger.warning("凭据校验返回 %s", r.status_code)
        raise HTTPException(status_code=503, detail="暂时无法校验登录，请稍后重试")
    data = r.json()
    p = Principal(
        id=str(data["user_id"]),
        kind="service" if data.get("kind") == "service" else "user",
        name=str(data.get("name") or ""),
        operations=frozenset(data.get("operations") or []),
    )
    _cache.put(key, p, settings.introspect_ttl_s)
    return p


async def current_principal(
    request: Request,
    settings: Annotated[KnowledgeSettings, Depends(get_settings)],
    x_user_id: Annotated[str | None, Header()] = None,
) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
        if not key.startswith(SERVICE_KEY_PREFIX):
            raise HTTPException(status_code=401, detail="凭据无效")
        body = {"service_key": key}
        if acting := request.headers.get("x-atlas-acting-user"):
            body["acting_user"] = acting
        return await _introspect(settings, body)

    if settings.auth_mode == "uc":
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="请先登录")
        if request.method not in _SAFE:
            cookie = request.cookies.get(CSRF_COOKIE)
            if not cookie or cookie != request.headers.get(CSRF_HEADER):
                raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面后重试")
        return await _introspect(settings, {"token": token})

    user = (x_user_id or "").strip() or settings.default_user
    return Principal(id=user, operations=DEV_OPERATIONS)


def require_internal(
    settings: Annotated[KnowledgeSettings, Depends(get_settings)],
    x_internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> None:
    expected = settings.internal_token.get_secret_value() if settings.internal_token else ""
    if not expected:
        if settings.auth_mode == "uc":
            raise HTTPException(status_code=401, detail="内部接口未配置令牌")
        return  # 本机开发
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=401, detail="内部接口需要服务间令牌")
