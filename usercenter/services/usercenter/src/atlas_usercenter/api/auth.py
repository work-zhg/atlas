"""登录、令牌、个人中心（c4-model/apigateway/code-auth.html、dynamic-web-jwt.html、dynamic-mobile-jwt.html）。

Web：登录、刷新都写 HttpOnly Cookie —— uc_access（访问 JWT，Path=/）、uc_refresh（刷新令牌，
     Path=<前缀>/api/v1/auth，平时的业务请求不带它）、uc_csrf（可读，写请求带进 X-UC-CSRF）。
手机：/auth/token 系列用 JSON 收发令牌，业务请求带 Authorization: Bearer。
★ 这些接口经网关原样透传（passthrough）：网关只看登录接口回的状态码
  （凭据不对一律 401，按 IP 数失败）。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ..auth.service import AuthService, Issued
from ..auth.tokens import TokenError, keyring
from ..common import Page
from ..db.models import App, Dept, User
from ..errors import Forbidden, UCError, Unauthorized
from ..org.service import OrgService
from ..perm.effective import can_access
from ..security import policy_text
from ..user.service import UserService
from .deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    LEGACY_SESSION_COOKIE,
    REFRESH_COOKIE,
    PrincipalDep,
    SessionDep,
    SettingsDep,
)
from .views import app_view, login_log_view, user_brief, user_view

router = APIRouter(prefix="/api/v1", tags=["auth"])


class LoginBody(BaseModel):
    account: str
    password: str
    #: 企业代码（租户）。只有一个租户的部署可以不填
    tenant: str | None = None


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


class MeBody(BaseModel):
    email: str | None = None
    phone: str | None = None


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


def _cookie_paths(settings: Any) -> tuple[str, str]:
    """(访问令牌、CSRF 的 Path, 刷新令牌的 Path)。"""
    return "/", f"{settings.cookie_path_prefix.rstrip('/')}/api/v1/auth"


def _set_session_cookies(
    response: Response, settings: Any, access: str, refresh: str | None, *, csrf: bool
) -> None:
    root, refresh_path = _cookie_paths(settings)
    max_age = settings.refresh_days * 86400
    # ★ 访问令牌的 Cookie 和刷新令牌活得一样久：过期的令牌照样带上来，
    #   网关回 TOKEN_EXPIRED，前端据此刷新
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=root,
    )
    if refresh is not None:
        response.set_cookie(
            REFRESH_COOKIE,
            refresh,
            max_age=max_age,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path=refresh_path,
        )
    if csrf:
        response.set_cookie(
            CSRF_COOKIE,
            secrets.token_urlsafe(24),
            max_age=max_age,
            httponly=False,
            secure=settings.cookie_secure,
            samesite="lax",
            path=root,
        )
    # 升级前的会话 Cookie：换成令牌后就不要了
    response.delete_cookie(LEGACY_SESSION_COOKIE, path=root)


def _clear_session_cookies(response: Response, settings: Any) -> None:
    root, refresh_path = _cookie_paths(settings)
    for name in (ACCESS_COOKIE, CSRF_COOKIE, LEGACY_SESSION_COOKIE):
        response.delete_cookie(name, path=root)
    response.delete_cookie(REFRESH_COOKIE, path=refresh_path)


async def _login(body: LoginBody, request: Request, session: Any, settings: Any) -> Issued:
    svc = AuthService(session, settings)
    try:
        return await svc.login(body.account, body.password, _client_ip(request), body.tenant)
    except UCError:
        # ★ 失败计数、锁定、登录日志必须落库：先提交再抛错（请求事务否则会回滚它们）
        await session.commit()
        raise


def _tokens_view(issued: Issued, settings: Any) -> dict[str, Any]:
    return {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": settings.access_minutes * 60,
        "refresh_token": issued.refresh_token,
        "refresh_expires_in": settings.refresh_days * 86400,
        "must_change_password": issued.must_change_password,
    }


@router.post("/auth/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Web 登录：令牌写进 Cookie。凭据不对一律 401（触发锁定的那一次也是）。"""
    issued = await _login(body, request, session, settings)
    _set_session_cookies(response, settings, issued.access_token, issued.refresh_token, csrf=True)
    return {"must_change_password": issued.must_change_password}


def _csrf_ok(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    return bool(cookie) and cookie == request.headers.get(CSRF_HEADER)


@router.post("/auth/refresh")
async def refresh(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Web 刷新：用 uc_refresh 换一对新的（每次都轮换）。401 = 要重新登录。

    ★ 前端单飞：同一时刻多个请求撞上 401 只刷新一次，刷新成功后重放原请求。
    """
    if not _csrf_ok(request):
        raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    try:
        issued = await AuthService(session, settings).refresh(request.cookies.get(REFRESH_COOKIE))
    except UCError:
        await session.commit()  # 发现被盗用时作废会话：要落库
        raise
    _set_session_cookies(response, settings, issued.access_token, issued.refresh_token, csrf=False)
    return {"must_change_password": issued.must_change_password}


@router.post("/auth/token")
async def token_login(
    body: LoginBody, request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """手机登录：令牌用 JSON 返回（App 存安全存储），不写 Cookie。和 Web 登录共用签发逻辑。"""
    return _tokens_view(await _login(body, request, session, settings), settings)


class RefreshBody(BaseModel):
    refresh_token: str


@router.post("/auth/token/refresh")
async def token_refresh(
    body: RefreshBody, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """手机刷新：换一对新的（每次都轮换）。401 = 要重新登录。"""
    try:
        issued = await AuthService(session, settings).refresh(body.refresh_token)
    except UCError:
        await session.commit()
        raise
    return _tokens_view(issued, settings)


@router.post("/auth/token/revoke")
async def token_revoke(
    body: RefreshBody, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    """手机退出登录：作废这个刷新令牌。

    它的访问令牌在网关最多再用 15 分钟；用户中心自己的接口立刻不认。
    """
    await AuthService(session, settings).logout(body.refresh_token)
    return {"ok": True}


@router.get("/auth/jwks")
async def jwks(response: Response, settings: SettingsDep) -> dict[str, Any]:
    """验签公钥集：网关的公钥环每 5 分钟经内部 Ingress 拉一次。"""
    response.headers["Cache-Control"] = "public, max-age=300"
    return keyring(settings).jwks()


_SAFE = {"GET", "HEAD", "OPTIONS"}


@router.get("/auth/verify", include_in_schema=False)
async def verify(request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    """旧网关的 auth_request（切到 API 网关之前还在用）：校验访问 JWT（Cookie 或 Bearer）或升级前的
    会话 → 204 + 响应头 X-Uid、X-Tenant-Id。

    网关把原请求的头（Cookie、Authorization、X-UC-CSRF）原样带过来，并用 X-Original-Method 告诉这里
    原请求的方法：Cookie 载体的写请求在这里校验 CSRF 双提交。
    未登录 / 令牌过期 / 须先改密 → 401（前端先刷新，刷新也 401 再跳登录页）；CSRF 不对 → 403。
    """
    authz = request.headers.get("authorization") or ""
    bearer = authz[7:].strip() if authz[:7].lower() == "bearer " else None
    v = await AuthService(session, settings).verify(
        access=bearer or request.cookies.get(ACCESS_COOKIE),
        legacy=None if bearer else request.cookies.get(LEGACY_SESSION_COOKIE),
    )
    if v.must_change_password:
        raise Unauthorized("PASSWORD_CHANGE_REQUIRED", "请先修改密码")
    method = (request.headers.get("x-original-method") or "GET").upper()
    if not bearer and method not in _SAFE and not _csrf_ok(request):
        raise Forbidden("CSRF_FAILED", "请求校验失败，请刷新页面后重试")
    return Response(
        status_code=204, headers={"X-Uid": str(v.user_uuid), "X-Tenant-Id": str(v.tenant_uuid)}
    )


@router.post("/auth/logout")
async def logout(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    """Web 登出：作废这次登录的刷新令牌，清掉 Cookie。刷新 Cookie 不在时按访问令牌里的会话作废。"""
    sid = None
    access = request.cookies.get(ACCESS_COOKIE)
    if access:
        try:
            sid = keyring(settings).verify(access).session_uuid
        except TokenError:
            sid = None
    await AuthService(session, settings).logout(
        request.cookies.get(REFRESH_COOKIE) or request.cookies.get(LEGACY_SESSION_COOKIE),
        session_uuid=sid,
    )
    _clear_session_cookies(response, settings)
    return {"ok": True}


@router.get("/auth/me")
async def me(principal: PrincipalDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    u = principal.user
    org = OrgService(session, principal.actor)
    dept = await org.get(u.dept_uuid)
    manager, source = await org.manager_of(u)
    return {
        "user": user_view(u, principal.credential, dept_path=await org.path_names(dept)),
        "must_change_password": principal.credential.must_change_password,
        "first_login": u.activated_at is None,
        "manager": {**user_brief(manager), "source": source} if manager else None,  # type: ignore[dict-item]
        "permissions": sorted(principal.permissions),
        "menus": principal.menus,
        "tenant": {
            "id": str(principal.tenant.uuid),
            "code": principal.tenant.code,
            "name": principal.tenant.name,
        },
        "password_policy": policy_text(settings),
        #: 与 password_policy 同一份规则的结构化形式：前端逐条打勾用
        "password_rules": {
            "min_len": settings.password_min_len,
            "need_upper": settings.password_need_upper,
            "need_digit": settings.password_need_digit,
            "need_symbol": settings.password_need_symbol,
        },
    }


@router.post("/auth/password")
async def change_password(
    body: PasswordBody,
    principal: PrincipalDep,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, bool]:
    """改密：别的登录（会话）全部作废，保留这一个。浏览器顺手换一个 mcp=false 的访问令牌；
    手机拿刷新令牌换一次即可。"""
    await UserService(session, principal.actor, settings).change_password(
        principal.user,
        body.old_password,
        body.new_password,
        keep_session=principal.session_uuid,
    )
    if principal.carrier == "COOKIE":
        access = AuthService(session, settings).access_token(
            principal.user, principal.session_uuid, False
        )
        _set_session_cookies(response, settings, access, None, csrf=False)
    return {"ok": True}


@router.get("/me/login-logs")
async def my_login_logs(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """本人最近 90 天的登录记录（个人中心「账号安全」）；失败的也列出，便于本人发现异常。"""
    logs = await UserService(session, principal.actor, settings).login_logs(
        principal.user.uuid, Page(page, size)
    )
    return [login_log_view(log) for log in logs]


@router.patch("/me")
async def update_me(
    body: MeBody, principal: PrincipalDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    u = await UserService(session, principal.actor, settings).update_self(principal.user, data)
    return user_view(u, principal.credential)


@router.get("/directory/users")
async def directory_users(
    principal: PrincipalDep,
    session: SessionDep,
    q: str | None = None,
    limit: int = 20,
    digital: bool = False,
) -> list[dict[str, Any]]:
    """通讯录：选人用（数据授权的 Owner 也要能选人），只含未停用用户与姓名、账号、部门名。

    digital=true：连数字员工一起（授权时可以选到它，部门名为空、带 user_type 供选人器分组）。
    """
    stmt = (
        select(User, Dept.name)
        .outerjoin(Dept, Dept.uuid == User.dept_uuid)
        .where(User.status == "active")
    )
    if not digital:
        stmt = stmt.where(User.user_type == "human")
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.name).like(like), User.account.like(like)))
    rows = (await session.execute(stmt.order_by(User.account).limit(min(max(limit, 1), 50)))).all()
    return [
        {
            "id": str(u.uuid),
            "name": u.name,
            "account": u.account,
            "dept": d,
            "user_type": u.user_type,
        }
        for u, d in rows
    ]


@router.get("/directory/depts")
async def directory_depts(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    """通讯录：部门树（选组织用），只含 id、上级、名称。"""
    rows = await session.scalars(select(Dept).order_by(Dept.depth, Dept.sort, Dept.name))
    return [
        {
            "id": str(d.uuid),
            "parent_id": str(d.parent_uuid) if d.parent_uuid else None,
            "name": d.name,
        }
        for d in rows
    ]


@router.get("/auth/tenant-required")
async def tenant_required(session: SessionDep) -> dict[str, Any]:
    """登录页是否要显示企业代码：只有一个租户的部署不显示，改为展示这家企业的名称。

    ★ 多租户时 tenant 为空：不在登录前透露有哪些企业。
    """
    from ..db.tenancy import platform_scope
    from ..tenant.service import sole_tenant

    with platform_scope(session):
        sole = await sole_tenant(session)
        return {"required": sole is None, "tenant": {"name": sole.name} if sole else None}


@router.get("/me/apps")
async def my_apps(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    apps = list(
        await session.scalars(
            select(App).where(App.is_builtin.is_(False), App.status == "active").order_by(App.id)
        )
    )
    return [app_view(a) for a in apps if await can_access(session, principal.user, a)]
