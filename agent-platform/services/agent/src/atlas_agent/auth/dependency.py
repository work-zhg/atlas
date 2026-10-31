"""每个请求解析一次主体（权限设计 §4；内网身份传递 §3）。

内网免令牌：前端 → 网关（只在这里校验登录）→ 本服务。身份在请求头里：
  X-Uid         谁在用（必填；网关按登录态写入，业务应用调用时写它的用户）
  X-Auth-Uid    按谁的权限判，缺省 = X-Uid
  X-Tenant-Id   租户，原样透传给下游
  X-Caller-App  直接调用方：gateway / teamflow / bench …；缺省 unknown

缺 X-Uid 一律 401；操作码、数据编码级授权按 X-Auth-Uid 取用户中心快照。
没有免鉴权的模式（原 dev 模式已删除）：本机与测试也走用户中心（测试里是替身）。

★ 登录、Cookie、CSRF 都在网关 + 用户中心（/api/v1/auth/verify）；这里不再处理。
★ 失败一律 401 / 403，原因只写日志。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from ..config import Settings, get_settings
from ..db.session import SessionDep
from ..errors import Unauthorized
from ..outbound import CallIdentity, use_identity
from .principal import Principal
from .service import AuthService

__all__ = ["current_principal", "uc_client"]


def uc_client(request: Request):  # type: ignore[no-untyped-def]
    """进程级用户中心客户端（create_app 里建）。"""
    return request.app.state.uc


def _uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw.strip())
    except ValueError as exc:
        raise Unauthorized("身份请求头格式错误") from exc


async def current_principal(
    request: Request,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    h = request.headers
    uid = _uuid(h.get("x-uid"))
    if uid is None:
        raise Unauthorized("请先登录")
    auth_uid = _uuid(h.get("x-auth-uid")) or uid
    tenant = (h.get("x-tenant-id") or "").strip() or None
    caller = (h.get("x-caller-app") or "").strip() or "unknown"
    # ★ 先设出站身份：下面取授权快照调用户中心时就要带上
    use_identity(CallIdentity(uid=uid, auth_uid=auth_uid, tenant_id=tenant))
    return await AuthService(session, settings, uc_client(request)).resolve(
        uid, auth_uid, caller_app=caller, tenant_id=tenant
    )
