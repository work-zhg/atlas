"""依赖：数据库会话、运营人员（登录态 + CSRF + 强制改密）、读写权限。

★ 只有一条权限规则（c4-model/opscenter/code-accounts.html · ActionPolicy）：运营观察员只能读；
  写操作、账号管理一律要运营管理员。接口声明自己是读（OperatorDep）还是写（AdminDep）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..accounts import Accounts, Principal
from ..db.session import get_session
from ..errors import Forbidden
from ..gateway import OpsClient
from ..settings import OpsSettings, get_settings

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "AdminDep",
    "ClientDep",
    "OperatorDep",
    "SessionDep",
    "SettingsDep",
]

SESSION_COOKIE = "ops_session"
CSRF_COOKIE = "ops_csrf"
CSRF_HEADER = "x-ops-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}
#: 临时密码登录后还能访问的接口
_CHANGE_ALLOWED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout"}

# ★ scope="function"：请求事务在响应发出之前提交（同用户中心）
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[OpsSettings, Depends(get_settings)]


def _client(request: Request) -> OpsClient:
    return request.app.state.ops_client  # type: ignore[no-any-return]


ClientDep = Annotated[OpsClient, Depends(_client)]


async def current_operator(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Principal:
    p = await Accounts(session, settings).resolve(request.cookies.get(SESSION_COOKIE))
    if request.method not in _SAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie or cookie != request.headers.get(CSRF_HEADER):
            raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    if p.must_change_password and request.url.path not in _CHANGE_ALLOWED:
        raise Forbidden("PASSWORD_CHANGE_REQUIRED", "请先修改密码")
    return p


OperatorDep = Annotated[Principal, Depends(current_operator)]


async def current_admin(p: OperatorDep) -> Principal:
    if not p.is_admin:
        raise Forbidden("ADMIN_REQUIRED", "运营观察员只能查看；这个操作要运营管理员")
    return p


AdminDep = Annotated[Principal, Depends(current_admin)]
