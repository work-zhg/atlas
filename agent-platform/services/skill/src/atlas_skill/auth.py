"""认证（设计 §14；权限设计 §9；内网身份传递 §3）。

内网免令牌：身份在请求头里（网关按登录态写入 X-Uid；atlas-server 运行时调用时写这一轮的身份）。

  /api/v1/*    用户（经网关）。缺 X-Uid 一律 401；操作码按 X-Auth-Uid（缺省 = X-Uid）取
               用户中心授权快照（以本应用的身份：X-Caller-App: skill）。不在本应用可访问
               范围内的人一律 403。登录、CSRF 在网关 + 用户中心，这里不再处理。
  /internal/*  运行时进程。网关一律 404（只在内网可达）；不再要服务间令牌。

★ 与运行时各自实现，不抽公共包：规则只有几十行，抽出来就是第三个所有服务都
  依赖的包，收益抵不上耦合。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request

from .settings import SkillSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_VIEW",
    "SKILL_REVIEW",
    "SKILL_UPLOAD",
    "Principal",
    "current_principal",
    "require_any",
]

#: 操作码（登记在本应用名下，register.py）
SKILL_UPLOAD = "skill:upload"
#: 审查、停用 / 启用、下架、重新检查
SKILL_REVIEW = "skill:review"
AUDIT_VIEW = "audit:view"

_FORWARD = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）：上传人、审查人
    user: str
    operations: frozenset[str] = field(default_factory=frozenset)

    def has_any(self, *ops: str) -> bool:
        return any(op in self.operations for op in ops)


class _Cache:
    """auth-uid → (过期时刻, 操作码)。只缓存成功结果；失败每次都问。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, frozenset[str]]] = {}

    def get(self, key: str) -> frozenset[str] | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: str, ops: frozenset[str], ttl: float) -> None:
        if len(self._rows) > 10_000:  # 防止无界增长
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, ops)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _operations(settings: SkillSettings, request: Request, auth_uid: str) -> frozenset[str]:
    hit = _cache.get(auth_uid)
    if hit is not None:
        return hit
    # 透传本次请求的身份（用户中心据此建立租户上下文）
    headers: dict[str, Any] = {
        out: request.headers[name] for name, out in _FORWARD.items() if request.headers.get(name)
    }
    headers["X-Caller-App"] = settings.uc_app_code
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, transport=_transport) as client:
            r = await client.get(
                f"{settings.uc_base_url.rstrip('/')}/open/v1/users/{auth_uid}/authz",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("取授权快照失败（用户中心不可达）：%s", exc)
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试") from exc
    if r.status_code in (401, 404):
        raise HTTPException(status_code=401, detail="请先登录")
    if r.status_code != 200:
        logger.warning("授权快照返回 %s", r.status_code)
        raise HTTPException(status_code=503, detail="暂时无法校验权限，请稍后重试")
    body = r.json()
    if (body.get("user") or {}).get("status") == "disabled":
        raise HTTPException(status_code=401, detail="账号已停用")
    if not body.get("can_access"):
        raise HTTPException(status_code=403, detail="没有使用「技能」应用的权限")
    ops = frozenset(body.get("permissions") or [])
    _cache.put(auth_uid, ops, settings.authz_ttl_s)
    return ops


async def current_principal(
    request: Request, settings: Annotated[SkillSettings, Depends(get_settings)]
) -> Principal:
    uid = (request.headers.get("x-uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    auth_uid = (request.headers.get("x-auth-uid") or "").strip() or uid
    return Principal(user=uid, operations=await _operations(settings, request, auth_uid))


def require_any(*ops: str):  # type: ignore[no-untyped-def]
    """有其中任一操作码即可（前端隐藏只负责「看不到」）。"""

    def dep(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.has_any(*ops):
            raise HTTPException(status_code=403, detail="没有权限执行该操作")
        return principal

    return dep
