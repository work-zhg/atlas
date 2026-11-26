"""依赖：数据库会话、管理台登录态（含 CSRF 与强制改密）、操作码鉴权、开放接口的应用身份。

★ 租户上下文在这里建立（租户设计 §06）：管理台取登录用户所属租户，开放接口取请求头里的租户
  （内网身份传递 §3）。之后的一切查询自动限定在该租户内 —— 租户永远不从请求参数读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from atlas_opsapi import OperatorStamp, OpsGuard
from fastapi import Depends, HTTPException, Request
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
    "ACCESS_COOKIE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "LEGACY_SESSION_COOKIE",
    "REFRESH_COOKIE",
    "OpenActorDep",
    "OpenAppDep",
    "OpenReadActorDep",
    "OpenReadAppDep",
    "OpsDep",
    "Principal",
    "PrincipalDep",
    "SessionDep",
    "SettingsDep",
    "require",
    "tenant_blocked",
]

#: 访问 JWT（Path=/，网关和用户中心都认）
ACCESS_COOKIE = "uc_access"
#: 刷新令牌（Path=<前缀>/api/v1/auth：平时的业务请求根本不带它）
REFRESH_COOKIE = "uc_refresh"
#: 升级前的会话 Cookie：切到访问 JWT 之前登录的人还带着它，到期前照样认
LEGACY_SESSION_COOKIE = "uc_session"
CSRF_COOKIE = "uc_csrf"
CSRF_HEADER = "x-uc-csrf"
_SAFE = {"GET", "HEAD", "OPTIONS"}
#: 须改密时仍可访问的接口
_CHANGE_ALLOWED = {"/api/v1/auth/me", "/api/v1/auth/password", "/api/v1/auth/logout"}

# ★ scope="function"：请求事务在响应发出之前提交。默认（request）是响应发出之后才执行 yield
#   后面的 commit —— 客户端拿到 200 立刻发下一个请求，会查不到刚写的数据（登录后立即调接口报
#   「登录已过期」）
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[UCSettings, Depends(get_settings)]


@dataclass
class Principal:
    user: User
    credential: Credential
    permissions: set[str]
    menus: list[dict]  # type: ignore[type-arg]
    tenant: Tenant
    #: 这次登录的会话（刷新令牌那一行）；经网关身份头进来的请求不知道，为空
    session_uuid: UUID | None
    #: GATEWAY（网关验过 JWT、写了身份头）| BEARER（手机）| COOKIE（浏览器直接带令牌来）
    carrier: str

    @property
    def actor(self) -> Actor:
        return Actor.user(self.user.uuid)

    def has(self, op: str) -> bool:
        return op in self.permissions


@dataclass(frozen=True)
class _Identity:
    user_uuid: UUID
    session_uuid: UUID | None
    carrier: str
    #: 网关写的租户：要和用户所属租户一致
    tenant_uuid: UUID | None = None


async def _identity(request: Request, session: AsyncSession, settings: UCSettings) -> _Identity:
    """这个请求是谁。依次看：网关写的身份头 → Bearer → Cookie 里的访问 JWT → 升级前的会话 Cookie。

    ★ 网关的 auth 路由已经验过 JWT、清掉了外部带来的身份头，只写 X-Uid、X-Tenant-Id 和
      X-Caller-App: gateway（和其他业务系统一样认它）。经网关透传过来的 /auth/*（我的信息、改密）
      和切换前的旧网关，带的是令牌本身，这里自己验签。
    """
    from ..auth.service import AuthService
    from ..auth.tokens import TokenError, keyring

    h = request.headers
    if h.get("x-caller-app") == "gateway" and h.get("x-uid") and h.get("x-tenant-id"):
        try:
            return _Identity(UUID(h["x-uid"]), None, "GATEWAY", UUID(h["x-tenant-id"]))
        except ValueError as exc:
            raise Unauthorized("NOT_LOGGED_IN", "请先登录") from exc
    authz = h.get("authorization")
    token, carrier = None, "COOKIE"
    if authz:
        scheme, _, token = authz.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthorized("TOKEN_INVALID", "登录凭据格式不对")
        token, carrier = token.strip(), "BEARER"
    else:
        token = request.cookies.get(ACCESS_COOKIE)
    if token:
        try:
            c = keyring(settings).verify(token)
        except TokenError as exc:
            msg = "登录已过期" if exc.code == "TOKEN_EXPIRED" else "登录凭据无效，请重新登录"
            raise Unauthorized(exc.code, msg) from exc
        return _Identity(c.user_uuid, c.session_uuid, carrier)
    legacy = request.cookies.get(LEGACY_SESSION_COOKIE)
    if legacy:
        with platform_scope(session):
            user, sess, _ = await AuthService(session, settings).resolve(legacy)
        return _Identity(user.uuid, sess.uuid, "COOKIE")
    raise Unauthorized("NOT_LOGGED_IN", "请先登录")


async def current_principal(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Principal:
    from ..tenant.service import get_tenant, tenant_usable

    ident = await _identity(request, session, settings)
    # 解析身份时还不知道租户：在平台范围内查到用户，随即切到他的租户
    with platform_scope(session):
        user = await session.scalar(select(User).where(User.uuid == ident.user_uuid))
        if user is None or user.status == "disabled":
            raise Unauthorized("NOT_LOGGED_IN", "账号已停用")
        if ident.tenant_uuid is not None and ident.tenant_uuid != user.tenant_uuid:
            raise Unauthorized("NOT_LOGGED_IN", "请先登录")
        # 访问令牌带着会话：会话被删了（登出、改密、重置密码）就立刻不认，不等令牌过期
        if ident.session_uuid is not None and not await session.scalar(
            select(Session.uuid).where(Session.uuid == ident.session_uuid)
        ):
            raise Unauthorized("NOT_LOGGED_IN", "登录已失效，请重新登录")
        cred = (
            await session.execute(select(Credential).where(Credential.user_uuid == user.uuid))
        ).scalar_one()
        tenant = await get_tenant(session, user.tenant_uuid)
    if not tenant_usable(tenant):
        raise Unauthorized("TENANT_DISABLED", "企业账号已停用或已到期，请联系管理员")
    set_tenant(session, tenant.uuid)
    # 浏览器直接带 Cookie 来的写请求要校 CSRF（经网关的已在网关校过；Bearer 不会被浏览器自动带上）
    if ident.carrier == "COOKIE" and request.method not in _SAFE:
        cookie = request.cookies.get(CSRF_COOKIE)
        if not cookie or cookie != request.headers.get(CSRF_HEADER):
            raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    if cred.must_change_password and request.url.path not in _CHANGE_ALLOWED:
        raise Forbidden("PASSWORD_CHANGE_REQUIRED", "请先修改密码")
    app = (
        await session.execute(select(App).where(App.uuid == await uc_app_uuid(session)))
    ).scalar_one()
    snap = await authz_snapshot(session, user, app)
    return Principal(
        user,
        cred,
        set(snap["permissions"]),
        snap["menus"],
        tenant,
        ident.session_uuid,
        ident.carrier,
    )


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


async def _resolve_open_app(request: Request, session: AsyncSession, *, blocked_ok: bool) -> App:
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
            if not blocked_ok:
                raise Forbidden("TENANT_DISABLED", "租户已停用或已到期")
            request.state.tenant_blocked = True
        await AppService(session, Actor.system()).require_enabled(app, tenant)
    set_tenant(session, tenant.uuid)
    return app


async def current_open_app(request: Request, session: SessionDep) -> App:
    """开放接口：请求头 X-Caller-App → 应用，租户见 _open_tenant（内网身份传递 §3、§11）。

    ★ 内网免令牌：调用方是谁只看 X-Caller-App（应用编码），网关不转发 /open/*。
      应用须存在、未停用，且已对该租户开通，否则 403。本请求随后的一切都限定在该租户内。
    ★ 租户停用、到期：这里一律 403。只读的权限查询用 OpenReadAppDep，回答「无权限」。
    """
    return await _resolve_open_app(request, session, blocked_ok=False)


async def current_open_read_app(request: Request, session: SessionDep) -> App:
    """只读的权限查询（授权快照、操作码判定、数据权限的查询）用这个：租户停用、到期时不报错，
    接口照常回答，结果是「无权限」（tenant_blocked）。

    ★ 业务系统不知道租户状态、只认权限：租户停用、到期后，它们问到的是「这个人没有权限」，
      和普通的无权限走同一条路（403），而不是「用户中心出错了」。
    """
    return await _resolve_open_app(request, session, blocked_ok=True)


def tenant_blocked(request: Request) -> bool:
    """本请求的租户已停用或已到期（只在 OpenReadAppDep 的接口里会是真）。"""
    return bool(getattr(request.state, "tenant_blocked", False))


OpenAppDep = Annotated[App, Depends(current_open_app)]
OpenReadAppDep = Annotated[App, Depends(current_open_read_app)]


async def open_actor(request: Request, app: OpenAppDep) -> Actor:
    """开放接口写操作的审计主体：应用，detail 里记请求头 X-Uid（谁在用）。"""
    return Actor.app(app.uuid, request.headers.get("x-uid") or None)


async def open_read_actor(request: Request, app: OpenReadAppDep) -> Actor:
    return Actor.app(app.uuid, request.headers.get("x-uid") or None)


OpenReadActorDep = Annotated[Actor, Depends(open_read_actor)]
OpenActorDep = Annotated[Actor, Depends(open_actor)]


async def current_ops(request: Request, settings: SettingsDep) -> Actor:
    """运营接口（/ops/v1）：只认运营中心 —— 核对签名和 5 分钟时间窗（atlas_opsapi）。

    ★ 网关不转发 /ops/*；内网请求头（X-Caller-App、X-Uid）在这里一概不认。
    → 审计主体：运营账号，detail 里记运营人员的名字和运营中心的租户操作。
    """
    try:
        stamp: OperatorStamp = await OpsGuard(settings.ops_api_key).verify(request)
    except HTTPException as exc:
        cls = Unauthorized if exc.status_code == 401 else Forbidden
        raise cls("OPS_NOT_ALLOWED", str(exc.detail)) from exc
    return Actor.ops(stamp.operator_uuid, stamp.operator_name, stamp.operation_uuid)


OpsDep = Annotated[Actor, Depends(current_ops)]
