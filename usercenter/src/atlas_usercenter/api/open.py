"""开放接口 /open/v1（应用接入设计 §14、权限设计 §16）。

★ 每个请求都限定在接口令牌所属应用：只能看到本应用的角色 / 菜单 / 数据编码、
  本应用可访问范围内的用户。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ..app.service import AppService
from ..audit import Actor
from ..common import Page, iso
from ..db.models import AppScopeDept, Dept, User
from ..errors import Invalid, NotFound, UCError
from ..org.service import OrgService
from ..perm.data import DataService
from ..perm.effective import authz_snapshot, can_access
from ..perm.service import GrantService  # noqa: F401  (保持模块加载顺序，避免循环导入)
from .deps import OpenAppDep, SessionDep, SettingsDep

router = APIRouter(prefix="/open/v1", tags=["open"])


class TokenBody(BaseModel):
    app_key: str
    app_secret: str


class VerifyBody(BaseModel):
    account: str
    password: str


class GrantItem(BaseModel):
    permission: Literal["READ", "WRITE", "OWNER"]
    subject_type: Literal["USER", "DEPT"]
    subject_id: UUID | None = None
    subject_account: str | None = None
    include_sub: bool = False


class DataBatchWrite(BaseModel):
    data_code: str
    data_id: str
    data_name: str | None = None
    grants: list[GrantItem]
    operator_id: UUID | None = None


class AclPatch(BaseModel):
    operator_id: UUID
    permission: Literal["READ", "WRITE", "OWNER"] | None = None
    include_sub: bool | None = None


class DataRename(BaseModel):
    data_code: str
    data_id: str
    data_name: str


class CheckBody(BaseModel):
    user_id: UUID
    permission: str


class DataWrite(BaseModel):
    data_code: str
    data_id: str
    data_name: str | None = None
    permission: Literal["READ", "WRITE", "OWNER"]
    subject_type: Literal["USER", "DEPT"]
    subject_id: UUID | None = None
    subject_account: str | None = None
    include_sub: bool = False
    operator_id: UUID | None = None


async def _user(session: SessionDep, user_id: UUID) -> User:
    u = (await session.execute(select(User).where(User.uuid == user_id))).scalar_one_or_none()
    if u is None:
        raise NotFound("USER_NOT_FOUND", "用户不存在")
    return u


async def _user_info(session: SessionDep, u: User) -> dict[str, Any]:
    org = OrgService(session, Actor.system())
    dept = await org.get(u.dept_uuid)
    manager, source = await org.manager_of(u)
    return {
        "id": str(u.uuid),
        "account": u.account,
        "name": u.name,
        "email": u.email,
        "status": u.status,
        "dept": {
            "id": str(dept.uuid),
            "path": " / ".join((await org.path_names(dept))[1:]) or dept.name,
        },
        "manager": {
            "id": str(manager.uuid),
            "name": manager.name,
            "account": manager.account,
            "source": source,
        }
        if manager
        else None,
    }


@router.post("/auth/token")
async def token(body: TokenBody, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    tok, ttl = await AppService(session, Actor.system(), settings).issue_token(
        body.app_key, body.app_secret
    )
    return {"access_token": tok, "token_type": "Bearer", "expires_in": ttl}


@router.post("/auth/verify-password")
async def verify_password(
    body: VerifyBody, request: Request, app: OpenAppDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """接入应用代为校验账号密码（本期没有 SSO；以后由 Keycloak 取代，届时下线）。

    ★ 失败计数、锁定、登录日志必须落库：失败时先提交再抛错。
    """
    from ..auth.service import AuthService

    try:
        user = await AuthService(session, settings).verify_for_app(
            body.account, body.password, request.client.host if request.client else None, app.name
        )
    except UCError:
        await session.commit()
        raise
    info = await _user_info(session, user)
    snap = await authz_snapshot(session, user, app)
    return {"user": info, **snap}


@router.get("/users/by-account/{account}")
async def user_by_account(account: str, app: OpenAppDep, session: SessionDep) -> dict[str, Any]:
    u = (
        await session.execute(select(User).where(User.account == account.lower()))
    ).scalar_one_or_none()
    if u is None:
        raise NotFound("USER_NOT_FOUND", "用户不存在")
    return await _user_info(session, u)


@router.get("/users/{user_id}")
async def user_by_id(user_id: UUID, app: OpenAppDep, session: SessionDep) -> dict[str, Any]:
    return await _user_info(session, await _user(session, user_id))


@router.get("/users")
async def list_users(
    app: OpenAppDep,
    session: SessionDep,
    q: str | None = None,
    dept_id: UUID | None = None,
    include_sub: bool = True,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """选人：只返回本应用可访问范围内的未停用用户。"""
    pg = Page(page, size)
    stmt = select(User).join(Dept, Dept.uuid == User.dept_uuid).where(User.status == "active")
    if not app.scope_all:
        scope = list(
            await session.scalars(
                select(Dept)
                .join(AppScopeDept, AppScopeDept.dept_uuid == Dept.uuid)
                .where(AppScopeDept.app_uuid == app.uuid)
            )
        )
        if not scope:
            return {"total": 0, "items": []}
        stmt = stmt.where(or_(*[Dept.path.like(d.path + "%") for d in scope]))
    if dept_id:
        d = await OrgService(session, Actor.system()).get(dept_id)
        stmt = (
            stmt.where(Dept.path.like(d.path + "%"))
            if include_sub
            else stmt.where(User.dept_uuid == d.uuid)
        )
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(User.name).like(like), User.account.like(like)))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    users = list(
        await session.scalars(stmt.order_by(User.account).offset(pg.offset).limit(pg.limit))
    )
    return {
        "total": total,
        "items": [
            {"id": str(u.uuid), "account": u.account, "name": u.name, "dept_id": str(u.dept_uuid)}
            for u in users
        ],
    }


@router.get("/depts/tree")
async def depts(app: OpenAppDep, session: SessionDep) -> list[dict[str, Any]]:
    rows = await session.scalars(select(Dept).order_by(Dept.depth, Dept.sort, Dept.name))
    return [
        {
            "id": str(d.uuid),
            "parent_id": str(d.parent_uuid) if d.parent_uuid else None,
            "name": d.name,
            "leader_id": str(d.leader_uuid) if d.leader_uuid else None,
        }
        for d in rows
    ]


@router.get("/users/{user_id}/authz")
async def authz(user_id: UUID, app: OpenAppDep, session: SessionDep) -> dict[str, Any]:
    u = await _user(session, user_id)
    snap = await authz_snapshot(session, u, app)
    from ..db.types import utcnow

    return {
        "user": {"id": str(u.uuid), "account": u.account, "name": u.name, "status": u.status},
        **snap,
        "computed_at": iso(utcnow()),
    }


@router.post("/authz/check")
async def check(body: CheckBody, app: OpenAppDep, session: SessionDep) -> dict[str, Any]:
    u = await _user(session, body.user_id)
    snap = await authz_snapshot(session, u, app)
    return {"allowed": body.permission in snap["permissions"], "roles": snap["roles"]}


async def _subject(session: SessionDep, body: DataWrite | GrantItem) -> tuple[str, UUID]:
    if body.subject_type == "DEPT":
        if body.subject_id is None:
            raise Invalid("VALIDATION_FAILED", "DEPT 授权须给 subject_id")
        return "dept", body.subject_id
    if body.subject_id is not None:
        return "user", body.subject_id
    if body.subject_account:
        u = (
            await session.execute(select(User).where(User.account == body.subject_account.lower()))
        ).scalar_one_or_none()
        if u is None:
            raise Invalid("SUBJECT_NOT_FOUND", "用户不存在")
        return "user", u.uuid
    raise Invalid("VALIDATION_FAILED", "USER 授权须给 subject_id 或 subject_account")


@router.post("/data-permissions", status_code=201)
async def data_write(body: DataWrite, app: OpenAppDep, session: SessionDep) -> dict[str, Any]:
    operator = await _user(session, body.operator_id) if body.operator_id else None
    return await DataService(session, Actor.app(app.uuid)).open_write(
        app,
        data_code=body.data_code,
        data_id=body.data_id,
        data_name=body.data_name,
        permission=body.permission,
        subject=await _subject(session, body),
        include_sub=body.include_sub,
        operator=operator,
    )


@router.post("/data-permissions/batch", status_code=201)
async def data_write_batch(
    body: DataBatchWrite, app: OpenAppDep, session: SessionDep
) -> dict[str, Any]:
    """一次写入多条授权（如新建团队时写入多名管理员与初始成员）。"""
    operator = await _user(session, body.operator_id) if body.operator_id else None
    grants = [(g.permission, await _subject(session, g), g.include_sub) for g in body.grants]
    return await DataService(session, Actor.app(app.uuid)).open_write_batch(
        app,
        data_code=body.data_code,
        data_id=body.data_id,
        data_name=body.data_name,
        grants=grants,
        operator=operator,
    )


@router.get("/data-permissions/acl")
async def data_acl(
    data_code: str, data_id: str, app: OpenAppDep, session: SessionDep
) -> list[dict[str, Any]]:
    return await DataService(session, Actor.app(app.uuid)).open_acl(
        app, data_code=data_code, data_id=data_id
    )


@router.patch("/data-permissions/acl/{acl_id}")
async def data_acl_patch(
    acl_id: UUID, body: AclPatch, app: OpenAppDep, session: SessionDep
) -> dict[str, bool]:
    await DataService(session, Actor.app(app.uuid)).open_update_acl(
        app,
        acl_id,
        await _user(session, body.operator_id),
        level=body.permission,
        include_sub=body.include_sub,
    )
    return {"ok": True}


@router.delete("/data-permissions/acl/{acl_id}")
async def data_acl_delete(
    acl_id: UUID, operator_id: UUID, app: OpenAppDep, session: SessionDep
) -> dict[str, bool]:
    await DataService(session, Actor.app(app.uuid)).open_delete_acl(
        app, acl_id, await _user(session, operator_id)
    )
    return {"ok": True}


@router.get("/data-permissions/holders")
async def data_holders(
    data_code: str, data_id: str, app: OpenAppDep, session: SessionDep, min: str = "READ"
) -> list[dict[str, Any]]:
    return await DataService(session, Actor.app(app.uuid)).open_holders(
        app, data_code=data_code, data_id=data_id, min_level=min
    )


@router.patch("/data-objects")
async def data_rename(body: DataRename, app: OpenAppDep, session: SessionDep) -> dict[str, bool]:
    await DataService(session, Actor.app(app.uuid)).open_rename(
        app, data_code=body.data_code, data_id=body.data_id, data_name=body.data_name
    )
    return {"ok": True}


@router.get("/data-permissions/check")
async def data_check(
    data_code: str, data_id: str, user_id: UUID, app: OpenAppDep, session: SessionDep
) -> dict[str, Any]:
    return await DataService(session, Actor.app(app.uuid)).open_check(
        app, data_code=data_code, data_id=data_id, user=await _user(session, user_id)
    )


@router.get("/data-permissions/accessible")
async def data_accessible(
    data_code: str,
    user_id: UUID,
    app: OpenAppDep,
    session: SessionDep,
    min: str = "READ",
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return await DataService(session, Actor.app(app.uuid)).open_accessible(
        app,
        data_code=data_code,
        user=await _user(session, user_id),
        min_level=min,
        page=Page(page, size),
    )


@router.delete("/data-permissions")
async def data_delete(
    data_code: str, data_id: str, app: OpenAppDep, session: SessionDep
) -> dict[str, int]:
    return await DataService(session, Actor.app(app.uuid)).open_delete(
        app, data_code=data_code, data_id=data_id
    )


_ = can_access
