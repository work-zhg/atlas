"""ORM → JSON。所有对外的 id 都是逻辑主键 uuid，自增 id 不出现在接口里。"""

from __future__ import annotations

from typing import Any

from ..common import display_status, iso
from ..db.models import App, Credential, DataType, LoginLog, Menu, Operation, Role, User

__all__ = [
    "app_view",
    "data_type_view",
    "menu_view",
    "op_view",
    "role_view",
    "user_brief",
    "user_view",
]


def user_brief(u: User | None) -> dict[str, Any] | None:
    if u is None:
        return None
    return {"id": str(u.uuid), "name": u.name, "account": u.account, "status": u.status}


def user_view(u: User, cred: Credential | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(u.uuid),
        "user_type": u.user_type,
        "account": u.account,
        "name": u.name,
        "email": u.email,
        "phone": u.phone,
        "dept_id": str(u.dept_uuid) if u.dept_uuid else None,
        "external_ref": u.external_ref,
        "description": u.description,
        "status": display_status(u, cred),
        "must_change_password": bool(
            cred and cred.must_change_password and u.activated_at is not None
        ),
        "last_login_at": iso(u.last_login_at),
        "created_at": iso(u.created_at),
        "disabled_reason": u.disabled_reason,
        "version": u.version,
        **extra,
    }


def login_log_view(log: LoginLog) -> dict[str, Any]:
    #: app = 经开放接口校验密码时的调用方应用；用户中心统一登录为空
    return {
        "time": iso(log.occurred_at),
        "ip": log.ip,
        "ok": log.ok,
        "reason": log.reason,
        "app": log.app_name,
    }


def app_view(a: App, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(a.uuid),
        "name": a.name,
        "code": a.code,
        "icon": a.icon,
        "description": a.description,
        "app_key": a.app_key,
        "secret_tail": a.secret_tail,
        # 范围属于「租户 × 应用」，由调用方按当前租户传入；内置应用对全员
        "scope_all": a.is_builtin,
        "status": a.status,
        "is_builtin": a.is_builtin,
        "home_url": a.home_url,
        "created_at": iso(a.created_at),
        "version": a.version,
        **extra,
    }


def op_view(o: Operation, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(o.uuid),
        "module": o.module,
        "code": o.code,
        "name": o.name,
        "source": o.source,
        **extra,
    }


def menu_view(m: Menu, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(m.uuid),
        "parent_id": str(m.parent_uuid) if m.parent_uuid else None,
        "type": m.type,
        "code": m.code,
        "name": m.name,
        "icon": m.icon,
        "path": m.path,
        "is_public": m.is_public,
        "sort": m.sort,
        "source": m.source,
        **extra,
    }


def role_view(r: Role, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(r.uuid),
        "app_id": str(r.app_uuid),
        "code": r.code,
        "name": r.name,
        "description": r.description,
        "is_builtin": r.is_builtin,
        #: 应用内置（所有租户共用，运营中心维护）；否则是本租户自定义的
        "shared": r.tenant_uuid is None,
        "version": r.version,
        **extra,
    }


def data_type_view(d: DataType, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(d.uuid),
        "app_id": str(d.app_uuid),
        "code": d.code,
        "name": d.name,
        "description": d.description,
        **extra,
    }
