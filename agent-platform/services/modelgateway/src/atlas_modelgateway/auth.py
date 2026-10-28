"""管理接口的身份与操作码（内网身份传递 §3）。

    X-Uid         谁在用（管理端经网关登录后由网关写入）
    X-Auth-Uid    按谁的权限判，缺省 = X-Uid
    X-Tenant-Id   租户，原样透传给用户中心
    X-Caller-App  直接调用方（atlas …）：内部接口按它判断能不能操作某个客户端类型

    MODEL_MANAGE  登记、修改、删除模型，纳入平台，设公有 / 私有，给私有模型授权；
                  客户端 Key 的新建、分配模型、重置、停用、启用、删除
                  「能用哪些模型」不靠操作码，靠数据权限（管理员持有「全部模型 · Owner」）

★ 出站透传：本服务调用户中心时带上本次请求的身份请求头，X-Caller-App 换成本服务的应用编码。
★ 与其它服务各自实现，不抽公共包（规则只有几十行）。
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import Depends, Request

from .errors import Forbidden, Unauthorized, Unavailable
from .settings import GatewaySettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL_MANAGE",
    "IdentityHeadersMiddleware",
    "ManagerDep",
    "Operator",
    "OperatorDep",
    "outbound_headers",
    "use_outbound",
]

MODEL_MANAGE = "model:manage"

#: 入站（小写）→ 出站请求头
_IDENTITY_HEADERS = {"x-uid": "X-Uid", "x-auth-uid": "X-Auth-Uid", "x-tenant-id": "X-Tenant-Id"}

_outbound: ContextVar[dict[str, str] | None] = ContextVar("modelgw_outbound_identity", default=None)


def outbound_headers() -> dict[str, str]:
    return {**(_outbound.get() or {}), "X-Caller-App": get_settings().uc_app_code}


def use_outbound(headers: dict[str, str] | None) -> None:
    """设置本任务之后调用户中心时带的身份（命令行、对账任务没有入站请求时用）。"""
    _outbound.set(headers)


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


@dataclass(frozen=True)
class Operator:
    #: 谁在操作（X-Uid）：登记模型时成为数据的所有者、授权时作为操作人交给用户中心
    uid: str
    #: 按谁的权限判（X-Auth-Uid）
    auth_uid: str
    operations: frozenset[str] = field(default_factory=frozenset)

    @property
    def can_manage(self) -> bool:
        return MODEL_MANAGE in self.operations


class _Cache:
    """auth-uid → (过期时刻, 操作码)。只缓存成功结果。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, frozenset[str]]] = {}

    def get(self, key: str) -> frozenset[str] | None:
        row = self._rows.get(key)
        if row is None or row[0] < time.monotonic():
            self._rows.pop(key, None)
            return None
        return row[1]

    def put(self, key: str, ops: frozenset[str], ttl: float) -> None:
        if len(self._rows) > 10_000:
            self._rows.clear()
        self._rows[key] = (time.monotonic() + ttl, ops)


_cache = _Cache()
#: 测试替换用（生产为 None，走真实网络）
_transport: httpx.AsyncBaseTransport | None = None


async def _operations(settings: GatewaySettings, auth_uid: str) -> frozenset[str]:
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
        raise Unavailable("暂时无法校验权限，请稍后重试") from exc
    if r.status_code in (401, 404):
        raise Unauthorized("请先登录")
    if r.status_code != 200:
        logger.warning("授权快照返回 %s：%s", r.status_code, r.text[:200])
        raise Unavailable("暂时无法校验权限，请稍后重试")
    body = r.json()
    if (body.get("user") or {}).get("status") == "disabled":
        raise Unauthorized("账号已停用")
    ops = frozenset(body.get("permissions") or []) if body.get("can_access") else frozenset()
    _cache.put(auth_uid, ops, settings.authz_ttl_s)
    return ops


async def current_operator(
    request: Request, settings: Annotated[GatewaySettings, Depends(get_settings)]
) -> Operator:
    h = request.headers
    uid = (h.get("x-uid") or "").strip()
    if not uid:
        raise Unauthorized("请先登录")
    auth_uid = (h.get("x-auth-uid") or "").strip() or uid
    return Operator(uid=uid, auth_uid=auth_uid, operations=await _operations(settings, auth_uid))


async def require_manager(operator: Annotated[Operator, Depends(current_operator)]) -> Operator:
    if not operator.can_manage:
        raise Forbidden("没有模型管理权限（model:manage）")
    return operator


OperatorDep = Annotated[Operator, Depends(current_operator)]
ManagerDep = Annotated[Operator, Depends(require_manager)]
