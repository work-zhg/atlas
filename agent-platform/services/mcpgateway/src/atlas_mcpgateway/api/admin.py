"""MCP 管理端接口（MCP 方案设计 §9.1）。经网关（nginx）登录后访问，身份在请求头里。

    服务       注册、修改、停用 / 启用、立即刷新          mcp:manage
    复核       待复核列表、批准 / 拒绝某一版定义          mcp:manage
    API Key    发放（可同时授服务）、重置、停用、启用、
               「有授权但没有 Key」的人                   mcp:manage
    授权       给人、部门、数字员工授「使用」 / 收回       该服务的所有者（用户中心校验）
    访问日志                                            mcp:manage；服务所有者看自己的服务

★ 授权写的是用户中心的数据权限，本服务只转交：操作人交给用户中心（operator_id），
  由它校验是不是所有者。MCP 管理员持有「全部 MCP 服务 · 所有者」，所以也可以。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from ..auth import ManagerDep, Operator, OperatorDep
from ..db.models import McpAccessLog
from ..errors import Forbidden, Invalid, NotFound
from ..perms import Grant, Permissions
from .deps import KeysDep, PermsDep, RegistryDep, SessionDep, UsableDep
from .schemas import (
    AccessLogOut,
    GrantIn,
    GrantPatch,
    KeyIn,
    KeyOut,
    MissingKeyOut,
    PendingReviewOut,
    ReviewIn,
    ServiceDetailOut,
    ServiceIn,
    ServiceOut,
    ServicePatch,
    ToolOut,
)
from .views import key_out, service_detail, service_out, tool_out

router = APIRouter(prefix="/api/v1", tags=["admin"])


async def _require_owner(perms: Permissions, operator: Operator, service: str) -> None:
    """MCP 管理员，或该服务的所有者。"""
    if operator.can_manage:
        return
    if await perms.level(operator.auth_uid, service) != "OWNER":
        raise Forbidden(f"只有 MCP 服务 {service} 的所有者或 MCP 管理员可以查看")


@router.get("/me")
async def me(operator: OperatorDep, perms: PermsDep) -> dict[str, Any]:
    """管理端据此决定显示哪些页面：MCP 管理员看全部；服务所有者只看自己的服务（授权、日志）。"""
    owned = await perms.owned(operator.auth_uid)
    return {
        "uid": operator.uid,
        "can_manage": operator.can_manage,
        "owned_services": sorted(owned.names),
        "owns_all": owned.all_services,
    }


# ───────────────────────────── 服务


@router.get("/services", response_model=list[ServiceOut])
async def list_services(
    operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[ServiceOut]:
    """MCP 管理员看全部；其他人看自己是所有者的服务（「服务详情 · 授权」页）。"""
    rows = await registry.list()
    if not operator.can_manage:
        owned = await perms.owned(operator.auth_uid)
        rows = [r for r in rows if owned.allows(r.name)]
    return [service_out(r, await registry.tools(r)) for r in rows]


@router.post("/services", response_model=ServiceDetailOut, status_code=201)
async def create_service(
    body: ServiceIn, operator: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> ServiceDetailOut:
    row = await registry.create(
        perms,
        name=body.name,
        display_name=body.display_name or body.name,
        description=body.description,
        upstream_name=body.upstream_name or body.name,
        review_required=body.review_required,
        call_timeout_s=body.call_timeout_s,
        credential_mode=body.credential_mode,
        actor=operator.uid,
    )
    return service_detail(row, await registry.tools(row, include_removed=True))


@router.get("/services/{name}", response_model=ServiceDetailOut)
async def get_service(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> ServiceDetailOut:
    row = await registry.get(name)
    await _require_owner(perms, operator, name)
    return service_detail(row, await registry.tools(row, include_removed=True))


@router.patch("/services/{name}", response_model=ServiceDetailOut)
async def update_service(
    name: str, body: ServicePatch, operator: ManagerDep, registry: RegistryDep, perms: PermsDep
) -> ServiceDetailOut:
    changes = body.model_dump(exclude_unset=True)
    for key in ("display_name", "description", "upstream_name", "review_required"):
        if key in changes and changes[key] is None:
            raise Invalid(f"{key} 不能为空")
    row = await registry.update(name, changes, perms=perms, actor=operator.uid)
    return service_detail(row, await registry.tools(row, include_removed=True))


@router.post("/services/{name}/disable", response_model=ServiceOut)
async def disable_service(name: str, operator: ManagerDep, registry: RegistryDep) -> ServiceOut:
    row = await registry.set_enabled(name, False, actor=operator.uid)
    return service_out(row, await registry.tools(row))


@router.post("/services/{name}/enable", response_model=ServiceOut)
async def enable_service(name: str, operator: ManagerDep, registry: RegistryDep) -> ServiceOut:
    row = await registry.set_enabled(name, True, actor=operator.uid)
    return service_out(row, await registry.tools(row))


@router.post("/services/{name}/refresh", response_model=ServiceDetailOut)
async def refresh_service(
    name: str, operator: ManagerDep, registry: RegistryDep
) -> ServiceDetailOut:
    row = await registry.refresh(await registry.get(name), manual=True)
    return service_detail(row, await registry.tools(row, include_removed=True))


# ───────────────────────────── 复核


@router.get("/reviews", response_model=list[PendingReviewOut])
async def pending_reviews(operator: ManagerDep, registry: RegistryDep) -> list[PendingReviewOut]:
    out = []
    for svc in await registry.list():
        for view in await registry.tools(svc):
            if view.status == "pending_review":
                out.append(PendingReviewOut(service=svc.name, tool=tool_out(svc.name, view)))
    return out


@router.post("/services/{name}/tools/{tool}/review", response_model=ToolOut)
async def review_tool(
    name: str, tool: str, body: ReviewIn, operator: ManagerDep, registry: RegistryDep
) -> ToolOut:
    view = await registry.review(
        name,
        tool,
        digest=body.digest,
        decision=body.decision,
        comment=body.comment,
        actor=operator.uid,
    )
    return tool_out(name, view)


# ───────────────────────────── 授权


@router.get("/services/{name}/grants")
async def list_grants(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    await registry.get(name)
    await _require_owner(perms, operator, name)
    return await perms.acl(name)


@router.post("/services/{name}/grants", status_code=201)
async def add_grant(
    name: str,
    body: GrantIn,
    operator: OperatorDep,
    registry: RegistryDep,
    perms: PermsDep,
    usable: UsableDep,
) -> dict[str, bool]:
    await registry.get(name)
    await perms.grant(
        name,
        operator.uid,
        [Grant(body.level, s.type, str(s.id), body.include_sub) for s in body.subjects],
    )
    # 授给部门的影响多少人不知道：本进程的判权缓存全清（别的副本等缓存过期）
    if all(s.type == "user" for s in body.subjects):
        for s in body.subjects:
            usable.forget(str(s.id))
    else:
        usable.forget()
    return {"ok": True}


@router.patch("/services/{name}/grants/{acl_id}")
async def update_grant(
    name: str,
    acl_id: str,
    body: GrantPatch,
    operator: OperatorDep,
    perms: PermsDep,
    usable: UsableDep,
) -> dict[str, bool]:
    await perms.update(acl_id, operator.uid, level=body.level, include_sub=body.include_sub)
    usable.forget()
    return {"ok": True}


@router.delete("/services/{name}/grants/{acl_id}")
async def revoke_grant(
    name: str, acl_id: str, operator: OperatorDep, perms: PermsDep, usable: UsableDep
) -> dict[str, bool]:
    await perms.revoke(acl_id, operator.uid)
    usable.forget()
    return {"ok": True}


@router.get("/services/{name}/holders")
async def holders(
    name: str, operator: OperatorDep, registry: RegistryDep, perms: PermsDep
) -> list[dict[str, Any]]:
    await registry.get(name)
    await _require_owner(perms, operator, name)
    return await perms.holders(name)


# ───────────────────────────── API Key


@router.get("/keys", response_model=list[KeyOut])
async def list_keys(operator: ManagerDep, keys: KeysDep) -> list[KeyOut]:
    return [key_out(k) for k in await keys.list()]


@router.post("/keys", response_model=KeyOut, status_code=201)
async def issue_key(
    body: KeyIn,
    operator: ManagerDep,
    keys: KeysDep,
    registry: RegistryDep,
    perms: PermsDep,
    usable: UsableDep,
) -> KeyOut:
    user = await perms.user(str(body.subject_uid))
    if user is None:
        raise NotFound("用户中心里没有这个用户")
    if user.get("status") != "active":
        raise Invalid("用户已停用，不能发放 Key")
    for name in body.services:
        await registry.get(name)
    row = await keys.issue(
        body.subject_uid,
        subject_type=str(user.get("user_type") or "human"),
        subject_name=str(user.get("name") or user.get("account") or ""),
        actor=operator.uid,
    )
    # 「给这把 Key 配置服务」写的是持有人的服务授权（§5.5）：一个事实源
    for name in body.services:
        await perms.grant(name, operator.uid, [Grant("READ", "user", str(body.subject_uid))])
    usable.forget(str(body.subject_uid))
    return key_out(row)


@router.post("/keys/{key_id}/reset", response_model=KeyOut)
async def reset_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.reset(key_id, actor=operator.uid))


@router.post("/keys/{key_id}/disable", response_model=KeyOut)
async def disable_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.disable(key_id, actor=operator.uid))


@router.post("/keys/{key_id}/enable", response_model=KeyOut)
async def enable_key(key_id: UUID, operator: ManagerDep, keys: KeysDep) -> KeyOut:
    return key_out(await keys.enable(key_id, actor=operator.uid))


@router.get("/keys/missing", response_model=list[MissingKeyOut])
async def missing_keys(
    operator: ManagerDep, keys: KeysDep, registry: RegistryDep, perms: PermsDep
) -> list[MissingKeyOut]:
    """有授权但没有可用 Key 的人：授权了也调不了（§5.5）。

    ★ 只看授给单条服务的（含经部门展开的）：「全部 MCP 服务」这类编码级授权的持有人不在
      用户中心的持有人列表里。
    """
    have = {str(k.subject_uid) for k in await keys.list() if k.status == "active"}
    found: dict[str, MissingKeyOut] = {}
    for svc in await registry.list():
        if svc.status != "enabled":
            continue
        for holder in await perms.holders(svc.name):
            user = holder.get("user") or {}
            uid = str(user.get("id") or "")
            if not uid or uid in have:
                continue
            row = found.setdefault(
                uid, MissingKeyOut(subject_uid=uid, name=str(user.get("name") or ""), services=[])
            )
            row.services.append(svc.name)
    return sorted(found.values(), key=lambda r: r.name)


# ───────────────────────────── 访问日志


@router.get("/access-log", response_model=list[AccessLogOut])
async def access_log(
    operator: OperatorDep,
    session: SessionDep,
    perms: PermsDep,
    service: str | None = None,
    subject: UUID | None = None,
    outcome: str | None = None,
    before: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[AccessLogOut]:
    if not operator.can_manage:
        if service is None:
            raise Forbidden("只能按自己是所有者的服务查看访问日志（带上 service）")
        await _require_owner(perms, operator, service)
    query = select(McpAccessLog).order_by(McpAccessLog.created_at.desc()).limit(limit)
    if service is not None:
        query = query.where(McpAccessLog.service == service)
    if subject is not None:
        query = query.where(McpAccessLog.subject_uid == subject)
    if outcome is not None:
        query = query.where(McpAccessLog.outcome == outcome)
    if before is not None:
        query = query.where(McpAccessLog.created_at < before)
    return [
        AccessLogOut(
            id=r.id,
            at=r.created_at,
            service=r.service,
            subject_uid=r.subject_uid,
            user_uid=r.user_uid,
            caller_app=r.caller_app,
            run_id=r.run_id,
            http_method=r.http_method,
            rpc_method=r.rpc_method,
            tool=r.tool,
            outcome=r.outcome,
            status=r.status,
            forwarded=r.forwarded,
            duration_ms=r.duration_ms,
        )
        for r in await session.scalars(query)
    ]
