"""认证（内网身份传递 §3、§8；知识库设计 §11）。

内网免令牌：身份在请求头里 —— 网关按登录态写入；atlas-server 运行时调用时写这一轮的身份。
  X-Uid         谁在用（建库人、上传人、检索记录里的发起人）
  X-Auth-Uid    按谁的权限判，缺省 = X-Uid：库级、文档级权限都按它（只剩这一套规则）
  X-Tenant-Id   租户，原样透传给用户中心
  X-Caller-App  直接调用方：gateway / atlas …

  uc 模式：缺 X-Uid → 401；操作码按 auth-uid 取用户中心授权快照（以 Atlas 应用的身份）
  dev 模式：缺 X-Uid 用 default_user，操作码全有

/internal/*：网关一律 404，不再要服务间令牌。/mcp：会话 Pod（不可信）调用，仍凭签名令牌。

★ 出站透传：本服务调用户中心、atlas-server 时原样带上本次请求的身份请求头，X-Caller-App 写
  atlas（知识库属于 Atlas 应用，数据编码登记在它名下）。
★ 与 atlas-server、atlas-config 各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request

from .settings import KnowledgeSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "CALLER_APP",
    "DEV_OPERATIONS",
    "IdentityHeadersMiddleware",
    "Principal",
    "current_principal",
    "outbound_headers",
    "use_identity",
]

#: 本服务调用户中心 / atlas-server 时的应用编码（知识库属于 Atlas 应用）
CALLER_APP = "atlas"
#: 入站（小写）→ 出站请求头
_IDENTITY_HEADERS = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}

#: 本服务关心的操作码（在 atlas-server auth/catalog.py 登记到用户中心）。
#: ★ 没有「管理全部知识库」的操作码：那是数据权限（用户中心的「全部知识库」授权，perms.py）
KB_OPERATIONS = frozenset({"kb:create", "kb:review"})
#: dev 模式的操作码
DEV_OPERATIONS = KB_OPERATIONS | {"chat:use", "agent:create"}


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）
    uid: str
    #: 按谁的权限判（X-Auth-Uid）。不给 = uid
    auth_uid: str = ""
    caller_app: str = "unknown"
    operations: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.auth_uid:
            object.__setattr__(self, "auth_uid", self.uid)

    def has(self, op: str) -> bool:
        return op in self.operations


# ───────────────────────────── 出站身份


_outbound: ContextVar[dict[str, str] | None] = ContextVar("kb_outbound_identity", default=None)


def use_identity(uid: str | None, auth_uid: str | None = None, tenant: str | None = None) -> None:
    """设置本任务之后的出站身份（MCP 调用按令牌里的身份设）。"""
    h = {}
    if uid:
        h["X-Uid"] = uid
        h["X-Auth-Uid"] = auth_uid or uid
    if tenant:
        h["X-Tenant-Id"] = tenant
    _outbound.set(h or None)


def outbound_headers() -> dict[str, str]:
    return {**(_outbound.get() or {}), "X-Caller-App": CALLER_APP}


class IdentityHeadersMiddleware:
    """每个请求：把入站的身份请求头设为出站身份（透传而不是重算），结束时还原。纯 ASGI。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        got = {
            _IDENTITY_HEADERS[k.decode("latin-1")]: v.decode("latin-1")
            for k, v in scope.get("headers") or []
            if k.decode("latin-1") in _IDENTITY_HEADERS and v
        }
        token = _outbound.set(got or None)
        try:
            await self.app(scope, receive, send)
        finally:
            _outbound.reset(token)


# ───────────────────────────── 授权快照（uc 模式）


class _Cache:
    """auth-uid → (过期时刻, 快照)。只缓存成功结果。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: str, snap: dict[str, Any], ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, snap)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _authz(settings: KnowledgeSettings, auth_uid: str) -> dict[str, Any]:
    hit = _cache.get(auth_uid)
    if hit is not None:
        return hit
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.get(
                f"{settings.uc_base_url.rstrip('/')}/open/v1/users/{auth_uid}/authz",
                headers=outbound_headers(),
            )
    except httpx.HTTPError as exc:
        logger.warning("取授权快照失败（用户中心不可达）：%s", exc)
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试") from exc
    if r.status_code in (401, 404):
        raise HTTPException(status_code=401, detail="请先登录")
    if r.status_code != 200:
        logger.warning("授权快照返回 %s：%s", r.status_code, r.text[:200])
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试")
    snap: dict[str, Any] = r.json()
    _cache.put(auth_uid, snap, settings.authz_ttl_s)
    return snap


async def current_principal(
    request: Request, settings: Annotated[KnowledgeSettings, Depends(get_settings)]
) -> Principal:
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    auth_uid = (h.get("x-auth-uid") or "").strip() or uid
    caller = (h.get("x-caller-app") or "").strip() or "unknown"
    if settings.auth_mode == "uc":
        if not uid:
            raise HTTPException(status_code=401, detail="请先登录")
        snap = await _authz(settings, auth_uid)
        user = snap.get("user") or {}
        if user.get("status") == "disabled":
            raise HTTPException(status_code=401, detail="账号已停用")
        if not snap.get("can_access"):
            raise HTTPException(status_code=403, detail="没有使用 Atlas 的权限")
        ops = frozenset(snap.get("permissions") or [])
        return Principal(uid=uid, auth_uid=auth_uid, caller_app=caller, operations=ops)
    uid = uid or settings.default_user
    return Principal(
        uid=uid, auth_uid=auth_uid or uid, caller_app=caller, operations=DEV_OPERATIONS
    )
