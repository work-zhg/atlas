"""依赖：数据库会话、管理台登录态（含 CSRF 与强制改密）、操作码鉴权、开放接口的应用身份。

★ 租户上下文在这里建立（租户设计 §06）：管理台取登录用户所属租户，开放接口取请求头里的租户
  （内网身份传递 §3）。之后的一切查询自动限定在该租户内 —— 租户永远不从请求参数读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app.service import AppService
from ..audit import Actor
from ..builtin import uc_app_uuid
from ..db.models import App, Credential, Session, Tenant, User
from ..db.session import get_session
from ..db.tenancy import platform_scope, set_tenant
from ..errors import Forbidden, NotFound, UCError, Unauthorized
from ..perm.effective import authz_snapshot
from ..settings import UCSettings, get_settings

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "OpenActorDep",
    "OpenAppDep",
    "Principal",
    "PrincipalDep",
    "SessionDep",
    "SettingsDep",
    "require",
]

SESSION_COOKIE = "uc_session"
CSRF_COOKIE = "uc_csrf"
CSRF_HEADER = "x-uc-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}
#: 须改密时仍可访问的接口
_CHANGE_ALLOWED = {"/api/v1/auth/me", "/api/v1/auth/password", "/api/v1/auth/logout"}

# ★ scope="function"：请求事务在响应发出之前提交。默认（request）是响应发出之后才执行 yield 后面的
#   commit —— 客户端拿到 200 立刻发下一个请求，会查不到刚写的数据（登录后立即调接口报「登录已过期」）
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[UCSettings, Depends(get_settings)]


@dataclass
class Principal:
    user: User
    session_row: Session
    credential: Credential
    permissions: set[str]
    menus: list[dict]  # type: ignore[type-arg]
    tenant: Tenant

    @property
    def actor(self) -> Actor:
        return Actor.user(self.user.uuid)

    def has(self, op: str) -> bool:
        return op in self.permissions


async def current_principal(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Principal:
    from ..auth.service import AuthService
    from ..tenant.service import get_tenant, tenant_usable

    # 解析会话时还不知道租户：在平台范围内查到用户，随即切到他的租户
    with platform_scope(session):
        user, sess, cred = await AuthService(session, settings).resolve(
            request.cookies.get(SESSION_COOKIE)
        )
        tenant = await get_tenant(session, user.tenant_uuid)
    if not tenant_usable(tenant):
        raise Unauthorized("TENANT_DISABLED", "企业账号已停用或已到期，请联系管理员")
    set_tenant(session, tenant.uuid)
    if request.method not in _SAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie or cookie != request.headers.get(CSRF_HEADER):
            raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    if cred.must_change_password and request.url.path not in _CHANGE_ALLOWED:
        raise Forbidden("PASSWORD_CHANGE_REQUIRED", "请先修改密码")
    app = (
        await session.execute(select(App).where(App.uuid == await uc_app_uuid(session)))
    ).scalar_one()
    snap = await authz_snapshot(session, user, app)
    return Principal(user, sess, cred, set(snap["permissions"]), snap["menus"], tenant)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(*ops: str):  # type: ignore[no-untyped-def]
    """操作码鉴权：前端隐藏只负责「看不到」，接口在这里再鉴权一次。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        missing = [op for op in ops if not principal.has(op)]
        if missing:
            raise Forbidden(
                "PERMISSION_DENIED", f"缺少操作权限：{'、'.join(missing)}", missing=missing
            )
        return principal

    return Depends(_dep)


async def _open_tenant(request: Request, session: AsyncSession) -> Tenant:
    """开放接口的租户：X-Tenant-Id → X-Uid 所属租户 → 唯一租户（单租户部署）。

    ★ 后台任务（TeamFlow 监督器、Atlas 的 run）只记得 uid，不另存租户：用户只属于一个租户，
      按 uid 推出来的与网关写入的一致。都没有且有多个租户时 400。
    """
    from ..tenant.service import get_tenant, sole_tenant

    raw = request.headers.get("x-tenant-id") or ""
    if raw:
        try:
            return await get_tenant(session, UUID(raw))
        except (ValueError, NotFound) as exc:
            raise UCError("TENANT_NOT_FOUND", "X-Tenant-Id 不是有效的租户") from exc
    if uid := request.headers.get("x-uid"):
        try:
            stmt = select(User.tenant_uuid).where(User.uuid == UUID(uid))
            tenant_uuid = await session.scalar(stmt)
        except ValueError:
            tenant_uuid = None
        if tenant_uuid is not None:
            return await get_tenant(session, tenant_uuid)
    tenant = await sole_tenant(session)
    if tenant is None:
        raise UCError("MISSING_TENANT", "缺少租户上下文（X-Tenant-Id）")
    return tenant


async def current_open_app(request: Request, session: SessionDep) -> App:
    """开放接口：请求头 X-Caller-App → 应用，租户见 _open_tenant（内网身份传递 §3、§11）。

    ★ 内网免令牌：调用方是谁只看 X-Caller-App（应用编码），网关不转发 /open/*。
      应用须存在、未停用，且已对该租户开通，否则 403。本请求随后的一切都限定在该租户内。
    """
    from ..tenant.service import tenant_usable

    code = (request.headers.get("x-caller-app") or "").strip()
    if not code:
        raise Unauthorized("MISSING_CALLER_APP", "缺少调用方应用（X-Caller-App）")
    with platform_scope(session):
        app = (await session.execute(select(App).where(App.code == code))).scalar_one_or_none()
        if app is None or app.status != "active":
            raise Forbidden("APP_NOT_ALLOWED", f"应用 {code} 不存在或已停用")
        tenant = await _open_tenant(request, session)
        if not tenant_usable(tenant):
            raise Forbidden("TENANT_DISABLED", "租户已停用或已到期")
        await AppService(session, Actor.system()).require_enabled(app, tenant)
    set_tenant(session, tenant.uuid)
    return app


OpenAppDep = Annotated[App, Depends(current_open_app)]


async def open_actor(request: Request, app: OpenAppDep) -> Actor:
    """开放接口写操作的审计主体：应用，detail 里记请求头 X-Uid（谁在用）。"""
    return Actor.app(app.uuid, request.headers.get("x-uid") or None)


OpenActorDep = Annotated[Actor, Depends(open_actor)]
