"""每个请求解析一次主体（权限设计 §4；内网身份传递 §3）。

内网免令牌：前端 → 网关（只在这里校验登录）→ 本服务。身份在请求头里：
  X-Uid         谁在用（必填；网关按登录态写入，业务应用调用时写它的用户）
  X-Auth-Uid    按谁的权限判，缺省 = X-Uid
  X-Tenant-Id   租户：以用户中心快照里用户所属的租户为准，写了却对不上 → 401
  X-Caller-App  直接调用方：gateway / teamflow / bench …；缺省 unknown

缺 X-Uid 一律 401；操作码、数据编码级授权按 X-Auth-Uid 取用户中心快照。租户已停用 403。
没有免鉴权的模式（原 dev 模式已删除）：本机与测试也走用户中心（测试里是替身）。

★ 登录、Cookie、CSRF 都在网关 + 用户中心（/api/v1/auth/verify）；这里不再处理。
★ 失败一律 401 / 403，原因只写日志。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from ..config import Settings, get_settings
from ..db.session import SessionDep
from ..errors import Unauthorized
from ..outbound import CallIdentity, use_identity
from .principal import Principal
from .service import AuthService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["current_principal", "principal_of", "uc_client"]


def uc_client(request: HTTPConnection):  # type: ignore[no-untyped-def]
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
    return await principal_of(request, session, settings)


async def principal_of(
    conn: HTTPConnection, session: AsyncSession, settings: Settings
) -> Principal:
    """按请求头解析主体。HTTP 请求经 current_principal；WebSocket（浏览器画面）连接时直接调它 ——
    长连接不该占着请求作用域的数据库会话。"""
    h = conn.headers
    uid = _uuid(h.get("x-uid"))
    if uid is None:
        raise Unauthorized("请先登录")
    auth_uid = _uuid(h.get("x-auth-uid")) or uid
    tenant = (h.get("x-tenant-id") or "").strip() or None
    caller = (h.get("x-caller-app") or "").strip() or "unknown"
    # ★ 先设出站身份：下面取授权快照调用户中心时就要带上
    use_identity(CallIdentity(uid=uid, auth_uid=auth_uid, tenant_id=tenant))
    uc = uc_client(conn)
    principal = await AuthService(session, settings, uc).resolve(
        uid, auth_uid, caller_app=caller, tenant_id=tenant
    )
    if uc is not None:
        # 按对象的数据权限（以数字员工为权限的会话按它判，auth/authz.py）：用到才查
        judged = principal.auth_uid

        async def data_check(code: str, data_id: str) -> str:
            return await uc.data_check(code, data_id, judged)

        principal = replace(principal, data_check=data_check)
    # 解析时已进入用户所属的租户；出站身份也换成核对过的租户（请求头可能没带）
    use_identity(CallIdentity(uid=uid, auth_uid=auth_uid, tenant_id=principal.tenant_id))
    # ★ 不查租户状态：租户停用、到期后，用户中心给的授权快照里没有 Atlas 的访问权（resolve 里 403）
    return principal
