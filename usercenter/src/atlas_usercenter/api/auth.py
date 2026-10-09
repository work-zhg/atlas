"""管理台登录、会话、个人中心。"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ..auth.service import AuthService
from ..db.models import App, Dept, User
from ..errors import UCError
from ..org.service import OrgService
from ..perm.effective import can_access
from ..security import policy_text
from ..user.service import UserService
from .deps import CSRF_COOKIE, SESSION_COOKIE, PrincipalDep, SessionDep, SettingsDep
from .views import app_view, user_brief, user_view

router = APIRouter(prefix="/api/v1", tags=["auth"])


class LoginBody(BaseModel):
    account: str
    password: str


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


@router.post("/auth/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    svc = AuthService(session, settings)
    try:
        result = await svc.login(body.account, body.password, _client_ip(request))
    except UCError:
        # ★ 失败计数、锁定、登录日志必须落库：先提交再抛错（请求事务否则会回滚它们）
        await session.commit()
        raise
    max_age = settings.session_hours * 3600
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(24),
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"must_change_password": result.must_change_password}


@router.post("/auth/logout")
async def logout(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await AuthService(session, settings).logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
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
        "password_policy": policy_text(settings),
    }


@router.post("/auth/password")
async def change_password(
    body: PasswordBody, principal: PrincipalDep, session: SessionDep, settings: SettingsDep
) -> dict[str, bool]:
    await UserService(session, principal.actor, settings).change_password(
        principal.user,
        body.old_password,
        body.new_password,
        keep_session=principal.session_row.uuid,
    )
    return {"ok": True}


@router.patch("/me")
async def update_me(
    body: MeBody, principal: PrincipalDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    u = await UserService(session, principal.actor, settings).update_self(principal.user, data)
    return user_view(u, principal.credential)


@router.get("/directory/users")
async def directory_users(
    principal: PrincipalDep, session: SessionDep, q: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """通讯录：选人用（数据授权的 Owner 也要能选人），只含未停用用户与姓名、账号、部门名。"""
    stmt = (
        select(User, Dept.name)
        .join(Dept, Dept.uuid == User.dept_uuid)
        .where(User.status == "active")
    )
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.name).like(like), User.account.like(like)))
    rows = (await session.execute(stmt.order_by(User.account).limit(min(max(limit, 1), 50)))).all()
    return [{"id": str(u.uuid), "name": u.name, "account": u.account, "dept": d} for u, d in rows]


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


@router.get("/me/apps")
async def my_apps(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    apps = list(
        await session.scalars(
            select(App).where(App.is_builtin.is_(False), App.status == "active").order_by(App.id)
        )
    )
    return [app_view(a) for a in apps if await can_access(session, principal.user, a)]
