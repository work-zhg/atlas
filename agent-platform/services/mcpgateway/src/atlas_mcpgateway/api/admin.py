"""MCP 管理端接口（MCP 网关原型）。经网关（nginx）登录后访问，身份在请求头里。

服务（写操作要 mcp:manage）
    GET    /api/v1/me                               能不能管理、是哪些服务的所有者
    GET    /api/v1/services                         管理员看全部；其他人看公开的、自己是
                                                    所有者的、自己的用户 Key 被授权了的
    POST   /api/v1/discover                         登记前预览：能转换成哪些工具
    POST   /api/v1/services                         登记
    GET    /api/v1/services/{name}                  详情（含工具）
    PATCH  /api/v1/services/{name}                  显示名称、描述、可见性
    PUT    /api/v1/services/{name}/connection       连接
    PUT    /api/v1/services/{name}/credential       上游凭据
    POST   /api/v1/services/{name}/enable|disable|refresh|test
    POST   /api/v1/services/{name}/rediscover|import   重新导入 / 重新发现
    POST   /api/v1/services/{name}/tools            REST：手工添加接口
    PATCH  /api/v1/services/{name}/tools/{tool}     改描述、副作用、映射
    DELETE /api/v1/services/{name}/tools/{tool}     REST / gRPC 删除
    POST   /api/v1/services/{name}/tools/{tool}/enable|disable
    POST   /api/v1/services/{name}/tools/{tool}/call
                                    试调：管理员任选一把 Key；其他人只能用自己的用户 Key

授权（服务所有者或 mcp:manage）
    GET    /api/v1/services/{name}/grants           所有者、授权的 Key
    PUT    /api/v1/services/{name}/grants/{key}     设置一把 Key 的范围
    DELETE /api/v1/services/{name}/grants/{key}     收回
    POST   /api/v1/services/{name}/owners           添加所有者
    DELETE /api/v1/services/{name}/owners/{user}    移除所有者
    GET    /api/v1/users?q=                         选人（添加所有者）
    GET    /api/v1/key-options                      授权时选 Key

API Key
    GET    /api/v1/keys/me                          我的用户 Key
    POST   /api/v1/keys/me                          申请（返回原文，只这一次）
    POST   /api/v1/keys/me/reset|block|unblock      重置（返回原文）/ 停用 / 启用
    DELETE /api/v1/keys/me                          删除
    GET    /api/v1/keys?key_type=&status=&q=        全部 Key（mcp:manage）
    POST   /api/v1/client-keys                      为智能体申请（mcp:manage）
    POST   /api/v1/keys/{id}/reset|block|unblock    智能体 Key 重置；停用 / 启用（mcp:manage）
    DELETE /api/v1/keys/{id}                        删除智能体 Key（mcp:manage）
    GET    /api/v1/keys/{id}/grants                 这把 Key 的权限（mcp:manage 或本人）
    PUT    /api/v1/keys/{id}/grants                 分配权限：只改自己能授权的服务
    GET    /api/v1/client-types                     客户端类型（mcp:manage）

★ 写操作在返回前提交：前端改完立刻重新拉列表，不能读到旧数据。
★ 授权写的是用户中心的数据权限，网关先判谁能授权，用户中心再按操作人校验一遍。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from opentelemetry import trace

from ..auth import ManagerDep, Operator, OperatorDep
from ..db.models import McpApiKey, McpService
from ..errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ..keys import KeyService
from ..perms import AccessCache, KeyAccess, Permissions
from ..upstreams import DiscoveryFailed
from ..upstreams.hub import decode_descriptor
from .deps import (
    AccessDep,
    DataPlaneDep,
    GrantsDep,
    KeysDep,
    PermsDep,
    RegistryDep,
    SessionDep,
    SettingsDep,
)
from .schemas import (
    ClientKeyIn,
    ClientTypeOut,
    ConnectionIn,
    CredentialIn,
    DiscoveredOut,
    DiscoverIn,
    GrantIn,
    ImportIn,
    KeyGrantsIn,
    KeyOut,
    KeyRefOut,
    KeySecretOut,
    OwnerIn,
    OwnerOut,
    RediscoverIn,
    ServerGrantsOut,
    ServiceDetailOut,
    ServiceIn,
    ServiceOut,
    ServicePatch,
    ServiceScopeOut,
    TestOut,
    ToolIn,
    ToolOut,
    ToolPatch,
    TryIn,
    TryOut,
)
from .views import (
    discovered_out,
    key_grant_out,
    key_out,
    key_ref,
    my_access,
    scopes_of,
    service_detail,
    service_out,
    tool_out,
)

router = APIRouter(prefix="/api/v1", tags=["admin"])
_tracer = trace.get_tracer("atlas_mcpgateway")


def _uid(operator: Operator) -> UUID | None:
    try:
        return UUID(operator.uid)
    except ValueError:
        return None


def _credential(body: CredentialIn) -> tuple[dict[str, Any], str | None]:
    raw = body.model_dump(exclude={"secret"})
    return raw, (body.secret or "").strip() or None


async def _safe_access(access: AccessCache, key: McpApiKey | None) -> KeyAccess | None:
    if key is None:
        return None
    try:
        return await access.get(str(key.subject_uid))
    except Unavailable:
        return None


class _Viewer:
    """当前用户：是不是管理员、是哪些服务的所有者、他的用户 Key 被授了什么。一个请求算一次。"""

    def __init__(
        self,
        operator: Operator,
        owned: frozenset[str],
        owns_all: bool,
        key: McpApiKey | None,
        access: KeyAccess | None,
    ) -> None:
        self.operator, self.owned, self.owns_all = operator, owned, owns_all
        self.key, self.access = key, access

    @classmethod
    async def load(
        cls, operator: Operator, perms: Permissions, keys: KeyService, access: AccessCache
    ) -> _Viewer:
        owned, owns_all = await perms.owned(operator.auth_uid)
        uid = _uid(operator)
        key = await keys.user_key(uid) if uid is not None else None
        return cls(operator, owned, owns_all, key, await _safe_access(access, key))

    def owner(self, svc: McpService) -> bool:
        return self.operator.can_manage or self.owns_all or svc.name in self.owned

    def my(self, svc: McpService) -> Any:
        return my_access(
            svc, owner=self.owner(svc), access=self.access, has_key=self.key is not None
        )

    def sees(self, svc: McpService) -> bool:
        if self.owner(svc):
            return True
        if svc.status != "enabled":
            return False
        return svc.visibility == "public" or self.my(svc).scope in ("all", "tools")


async def _require_assign(grants: Any, operator: Operator, name: str) -> None:
    await grants.require_assign(operator.auth_uid, name, manager=operator.can_manage)


# ───────────────────────────── 我


@router.get("/me")
async def me(
    operator: OperatorDep, perms: PermsDep, keys: KeysDep, settings: SettingsDep
) -> dict[str, Any]:
    owned, owns_all = await perms.owned(operator.auth_uid)
    uid = _uid(operator)
    return {
        "uid": operator.uid,
        "can_manage": operator.can_manage,
        "owned_services": sorted(owned),
        "owns_all": owns_all,
        "has_key": uid is not None and await keys.user_key(uid) is not None,
        "public_url": settings.public_url.rstrip("/") or None,
    }


# ───────────────────────────── 服务


@router.get("/services", response_model=list[ServiceOut])
async def list_services(
    operator: OperatorDep, registry: RegistryDep, perms: PermsDep, keys: KeysDep, access: AccessDep
) -> list[ServiceOut]:
    viewer = await _Viewer.load(operator, perms, keys, access)
    return [
        service_out(svc, await registry.tools(svc), viewer.my(svc))
        for svc in await registry.list()
        if viewer.sees(svc)
    ]


@router.post("/discover", response_model=DiscoveredOut)
async def discover(body: DiscoverIn, operator: ManagerDep, registry: RegistryDep) -> DiscoveredOut:
    raw, secret = _credential(body.credential)
    try:
        descriptor = decode_descriptor(body.descriptor)
    except DiscoveryFailed as exc:
        raise Invalid(str(exc)) from exc
    found = await registry.discover(
        kind=body.kind,
        connection=body.connection,
        credential=raw,
        secret=secret,
        descriptor=descriptor,
    )
    return discovered_out(found)


@router.post("/services", response_model=ServiceDetailOut, status_code=201)
async def create_service(
    body: ServiceIn, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> ServiceDetailOut:
    raw, secret = _credential(body.credential)
    try:
        descriptor = decode_descriptor(body.descriptor)
    except DiscoveryFailed as exc:
        raise Invalid(str(exc)) from exc
    row = await registry.create(
        kind=body.kind,
        name=body.name,
        display_name=body.display_name or body.name,
        description=body.description,
        visibility=body.visibility,
        connection=body.connection,
        credential=raw,
        secret=secret,
        pass_identity=body.pass_identity,
        selected=body.selected,
        descriptor=descriptor,
        actor=operator.uid,
    )
    await session.commit()
    return service_detail(
        row,
        await registry.tools(row, include_removed=True),
        my=my_access(row, owner=True, access=None, has_key=True),
        can_manage=True,
        can_assign=True,
    )


async def _detail(
    name: str,
    operator: Operator,
    registry: Any,
    perms: Permissions,
    keys: KeyService,
    access: AccessCache,
) -> ServiceDetailOut:
    row = await registry.get(name)
    viewer = await _Viewer.load(operator, perms, keys, access)
    if not viewer.sees(row):
        raise NotFound(f"MCP 服务 {name} 不存在", service=name)
    return service_detail(
        row,
        await registry.tools(row, include_removed=True),
        my=viewer.my(row),
        can_manage=operator.can_manage,
        can_assign=viewer.owner(row),
    )


@router.get("/services/{name}", response_model=ServiceDetailOut)
async def get_service(
    name: str,
    operator: OperatorDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
) -> ServiceDetailOut:
    return await _detail(name, operator, registry, perms, keys, access)


@router.patch("/services/{name}", response_model=ServiceDetailOut)
async def update_service(
    name: str,
    body: ServicePatch,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    changes = body.model_dump(exclude_unset=True)
    if "display_name" in changes and not (changes["display_name"] or "").strip():
        raise Invalid("显示名称不能为空")
    await registry.update(name, changes, actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, registry, perms, keys, access)


@router.put("/services/{name}/connection", response_model=ServiceDetailOut)
async def update_connection(
    name: str,
    body: ConnectionIn,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    await registry.update_connection(
        name, body.connection, pass_identity=body.pass_identity, actor=operator.uid
    )
    await session.commit()
    return await _detail(name, operator, registry, perms, keys, access)


@router.put("/services/{name}/credential", response_model=ServiceDetailOut)
async def update_credential(
    name: str,
    body: CredentialIn,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    raw, secret = _credential(body)
    await registry.update_credential(name, raw, secret, actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, registry, perms, keys, access)


async def _service_action(
    name: str,
    action: str,
    operator: Operator,
    registry: Any,
    perms: Permissions,
    keys: KeyService,
    access: AccessCache,
    session: Any,
) -> ServiceDetailOut:
    if action == "refresh":
        await registry.refresh(await registry.get(name), manual=True)
    else:
        await registry.set_enabled(name, action == "enable", actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, registry, perms, keys, access)


@router.post("/services/{name}/enable", response_model=ServiceDetailOut)
async def enable_service(
    name: str,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "enable", operator, registry, perms, keys, access, session)


@router.post("/services/{name}/disable", response_model=ServiceDetailOut)
async def disable_service(
    name: str,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "disable", operator, registry, perms, keys, access, session)


@router.post("/services/{name}/refresh", response_model=ServiceDetailOut)
async def refresh_service(
    name: str,
    operator: ManagerDep,
    registry: RegistryDep,
    perms: PermsDep,
    keys: KeysDep,
    access: AccessDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "refresh", operator, registry, perms, keys, access, session)


@router.post("/services/{name}/test", response_model=TestOut)
async def test_service(name: str, operator: ManagerDep, registry: RegistryDep) -> TestOut:
    """测试连接：按保存的连接和凭据发现一次工具（不写库）。"""
    row = await registry.get(name)
    if row.kind == "rest" and not row.connection.get("spec_url"):
        try:
            r = await registry.upstreams.http.get(row.connection["base_url"], timeout=10.0)
        except Exception as exc:
            return TestOut(ok=False, detail=f"连不上：{type(exc).__name__}: {exc}")
        return TestOut(ok=True, detail=f"能连上（HTTP {r.status_code}）")
    try:
        found = await registry.discover(
            kind=row.kind,
            connection=row.connection,
            credential=row.credential,
            secret=None,
            descriptor=None,
            service=row.name,
            previous=row,
        )
    except Invalid as exc:
        return TestOut(ok=False, detail=exc.message)
    count = len(found.definitions or found.operations or found.methods)
    return TestOut(ok=True, detail=f"连接正常，发现 {count} 个工具")


@router.post("/services/{name}/rediscover", response_model=DiscoveredOut)
async def rediscover(
    name: str, body: RediscoverIn, operator: ManagerDep, registry: RegistryDep
) -> DiscoveredOut:
    row = await registry.get(name)
    if row.kind == "mcp":
        raise Invalid("MCP 代理的工具从上游拉取，用「立即刷新」")
    try:
        descriptor = decode_descriptor(body.descriptor)
    except DiscoveryFailed as exc:
        raise Invalid(str(exc)) from exc
    found = await registry.discover(
        kind=row.kind,
        connection=row.connection,
        credential=row.credential,
        secret=None,
        descriptor=descriptor,
        service=row.name,
        previous=row,
    )
    existing = {t.name for t in await registry.tools(row, include_removed=True)}
    return discovered_out(found, existing=existing)


@router.post("/services/{name}/import", response_model=list[str])
async def import_tools(
    name: str, body: ImportIn, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> list[str]:
    try:
        descriptor = decode_descriptor(body.descriptor)
    except DiscoveryFailed as exc:
        raise Invalid(str(exc)) from exc
    added = await registry.add_discovered(
        name, body.selected, descriptor=descriptor, actor=operator.uid
    )
    await session.commit()
    return added


# ───────────────────────────── 工具


@router.post("/services/{name}/tools", response_model=ToolOut, status_code=201)
async def add_tool(
    name: str, body: ToolIn, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> ToolOut:
    tool = await registry.add_tool(
        name,
        tool_name=body.name,
        description=body.description,
        effect=body.effect,
        mapping=body.mapping,
        actor=operator.uid,
    )
    await session.commit()
    return tool_out(name, tool)


@router.patch("/services/{name}/tools/{tool}", response_model=ToolOut)
async def update_tool(
    name: str,
    tool: str,
    body: ToolPatch,
    operator: ManagerDep,
    registry: RegistryDep,
    session: SessionDep,
) -> ToolOut:
    row = await registry.update_tool(
        name,
        tool,
        description=body.description,
        effect=body.effect,
        mapping=body.mapping,
        actor=operator.uid,
    )
    await session.commit()
    return tool_out(name, row)


@router.delete("/services/{name}/tools/{tool}")
async def delete_tool(
    name: str, tool: str, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> dict[str, bool]:
    await registry.delete_tool(name, tool, actor=operator.uid)
    await session.commit()
    return {"deleted": True}


@router.post("/services/{name}/tools/{tool}/enable", response_model=ToolOut)
async def enable_tool(
    name: str, tool: str, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> ToolOut:
    row = await registry.set_tool_status(name, tool, active=True, actor=operator.uid)
    await session.commit()
    return tool_out(name, row)


@router.post("/services/{name}/tools/{tool}/disable", response_model=ToolOut)
async def disable_tool(
    name: str, tool: str, operator: ManagerDep, registry: RegistryDep, session: SessionDep
) -> ToolOut:
    row = await registry.set_tool_status(name, tool, active=False, actor=operator.uid)
    await session.commit()
    return tool_out(name, row)


@router.post("/services/{name}/tools/{tool}/call", response_model=TryOut)
async def try_call(
    name: str,
    tool: str,
    body: TryIn,
    operator: OperatorDep,
    keys: KeysDep,
    plane: DataPlaneDep,
) -> TryOut:
    """试调：和真实调用一样认 Key、判权、校验参数、调上游。会真的执行有副作用的工具。"""
    from ..dataplane import Rejection

    key = await keys.get(body.key_id)
    if not operator.can_manage and not (
        key.key_type == "user" and str(key.user_uuid) == operator.uid
    ):
        raise Forbidden("只能用自己的用户 Key 试调")
    with _tracer.start_as_current_span(f"try {name}/{tool}") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        try:
            caller = await plane.admit(
                name, key.secret if key.status == "active" else "", user_uid=operator.uid
            )
            result = await plane.execute(caller, tool, body.arguments)
        except Rejection as r:
            return TryOut(error=r.error(name), trace_id=trace_id)
    return TryOut(result=result, trace_id=trace_id)


# ───────────────────────────── 授权


@router.get("/services/{name}/grants", response_model=ServerGrantsOut)
async def list_grants(
    name: str, operator: OperatorDep, registry: RegistryDep, grants: GrantsDep
) -> ServerGrantsOut:
    svc = await registry.get(name)
    await _require_assign(grants, operator, name)
    found = await grants.of_server(svc)
    return ServerGrantsOut(
        owners=[OwnerOut(user_id=e.subject_id, name=e.subject_name) for e in found.owners],
        keys=[key_grant_out(g) for g in found.keys],
    )


@router.put("/services/{name}/grants/{key_id}", response_model=ServerGrantsOut)
async def set_grant(
    name: str,
    key_id: UUID,
    body: GrantIn,
    operator: OperatorDep,
    registry: RegistryDep,
    keys: KeysDep,
    grants: GrantsDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await registry.get(name)
    await _require_assign(grants, operator, name)
    key = await keys.get(key_id)
    await grants.set_key(svc, key, scope=body.scope, tools=body.tools, operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, registry, grants)


@router.delete("/services/{name}/grants/{key_id}", response_model=ServerGrantsOut)
async def revoke_grant(
    name: str,
    key_id: UUID,
    operator: OperatorDep,
    registry: RegistryDep,
    keys: KeysDep,
    grants: GrantsDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await registry.get(name)
    await _require_assign(grants, operator, name)
    key = await keys.get(key_id)
    await grants.set_key(svc, key, scope="none", tools=[], operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, registry, grants)


@router.post("/services/{name}/owners", response_model=ServerGrantsOut)
async def add_owner(
    name: str,
    body: OwnerIn,
    operator: OperatorDep,
    registry: RegistryDep,
    grants: GrantsDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await registry.get(name)
    await _require_assign(grants, operator, name)
    await grants.add_owner(svc, str(body.user_id), operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, registry, grants)


@router.delete("/services/{name}/owners/{user_id}", response_model=ServerGrantsOut)
async def remove_owner(
    name: str,
    user_id: UUID,
    operator: OperatorDep,
    registry: RegistryDep,
    grants: GrantsDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await registry.get(name)
    await _require_assign(grants, operator, name)
    await grants.remove_owner(svc, str(user_id), operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, registry, grants)


async def _require_grantor(operator: Operator, perms: Permissions) -> None:
    """MCP 管理员，或至少是一个服务的所有者。"""
    if operator.can_manage:
        return
    owned, owns_all = await perms.owned(operator.auth_uid)
    if not (owns_all or owned):
        raise Forbidden("只有服务所有者或 MCP 管理员可以授权")


@router.get("/users")
async def search_users(
    operator: OperatorDep, perms: PermsDep, q: str = Query(min_length=1, max_length=50)
) -> list[dict[str, Any]]:
    await _require_grantor(operator, perms)
    return [
        {"id": u.get("id"), "name": u.get("name"), "account": u.get("account")}
        for u in await perms.search_users(q)
    ]


@router.get("/key-options", response_model=list[KeyRefOut])
async def key_options(operator: OperatorDep, perms: PermsDep, keys: KeysDep) -> list[KeyRefOut]:
    await _require_grantor(operator, perms)
    return [key_ref(k) for k in await keys.list()]


# ───────────────────────────── API Key


async def _with_grants(rows: list[McpApiKey], registry: Any, access: AccessCache) -> list[KeyOut]:
    services = {s.name: s for s in await registry.list()}
    found = await asyncio.gather(*(_safe_access(access, k) for k in rows))
    return [key_out(k, scopes_of(a, services)) for k, a in zip(rows, found, strict=True)]


async def _my_key(keys: KeyService, operator: Operator) -> McpApiKey:
    uid = _uid(operator)
    key = await keys.user_key(uid) if uid is not None else None
    if key is None:
        raise NotFound("你还没有用户 Key")
    return key


@router.get("/keys/me", response_model=KeyOut | None)
async def my_key(
    operator: OperatorDep, keys: KeysDep, registry: RegistryDep, access: AccessDep
) -> KeyOut | None:
    uid = _uid(operator)
    key = await keys.user_key(uid) if uid is not None else None
    if key is None:
        return None
    return (await _with_grants([key], registry, access))[0]


@router.post("/keys/me", response_model=KeySecretOut, status_code=201)
async def apply_key(operator: OperatorDep, keys: KeysDep, session: SessionDep) -> KeySecretOut:
    uid = _uid(operator)
    if uid is None:
        raise Invalid("当前身份不是用户中心的用户")
    key, secret = await keys.apply(uid, actor=operator.uid)
    await session.commit()
    return KeySecretOut(**key_out(key).model_dump(), secret=secret)


@router.post("/keys/me/reset", response_model=KeySecretOut)
async def reset_my_key(operator: OperatorDep, keys: KeysDep, session: SessionDep) -> KeySecretOut:
    key = await _my_key(keys, operator)
    secret = await keys.reset(key, actor=operator.uid)
    await session.commit()
    return KeySecretOut(**key_out(key).model_dump(), secret=secret)


@router.post("/keys/me/block", response_model=KeyOut)
async def block_my_key(operator: OperatorDep, keys: KeysDep, session: SessionDep) -> KeyOut:
    key = await _my_key(keys, operator)
    await keys.block(key, actor=operator.uid)
    await session.commit()
    return key_out(key)


@router.post("/keys/me/unblock", response_model=KeyOut)
async def unblock_my_key(operator: OperatorDep, keys: KeysDep, session: SessionDep) -> KeyOut:
    key = await _my_key(keys, operator)
    await keys.unblock(key, actor=operator.uid)
    await session.commit()
    return key_out(key)


@router.delete("/keys/me")
async def delete_my_key(
    operator: OperatorDep, keys: KeysDep, session: SessionDep
) -> dict[str, bool]:
    await keys.delete(await _my_key(keys, operator), actor=operator.uid)
    await session.commit()
    return {"deleted": True}


@router.get("/keys", response_model=list[KeyOut])
async def list_keys(
    operator: ManagerDep,
    keys: KeysDep,
    registry: RegistryDep,
    access: AccessDep,
    key_type: str | None = Query(default=None, pattern="^(user|client)$"),
    status: str | None = Query(default=None, pattern="^(active|blocked)$"),
    q: str | None = None,
) -> list[KeyOut]:
    rows = await keys.list(key_type=key_type, status=status, q=q)
    return await _with_grants(rows, registry, access)


@router.get("/client-types", response_model=list[ClientTypeOut])
async def client_types(_: ManagerDep, settings: SettingsDep) -> list[ClientTypeOut]:
    return [ClientTypeOut(code=c, apps=list(apps)) for c, apps in settings.client_types.items()]


@router.post("/client-keys", response_model=KeyOut, status_code=201)
async def create_client_key(
    body: ClientKeyIn,
    operator: ManagerDep,
    keys: KeysDep,
    settings: SettingsDep,
    session: SessionDep,
) -> KeyOut:
    if body.client_type not in settings.client_types:
        raise Invalid(f"没有登记的客户端类型：{body.client_type}")
    key, created = await keys.ensure_client(
        client_type=body.client_type,
        client_code=body.client_code,
        name=body.name.strip(),
        owner=str(body.owner_uid or operator.uid),
        actor=operator.uid,
    )
    if not created:
        raise Conflict(f"{body.client_type}:{body.client_code} 已经有 Key 了")
    await session.commit()
    return key_out(key)


async def _key_action(
    key_id: UUID, action: str, operator: Operator, keys: KeyService, session: Any
) -> KeyOut:
    key = await keys.get(key_id)
    if action == "reset":
        if key.key_type == "user":
            raise Forbidden("用户 Key 只有本人能重置（原文只显示给本人）")
        await keys.reset(key, actor=operator.uid)
    elif action == "block":
        await keys.block(key, actor=operator.uid)
    else:
        await keys.unblock(key, actor=operator.uid)
    await session.commit()
    return key_out(key)


@router.post("/keys/{key_id}/reset", response_model=KeyOut)
async def reset_key(
    key_id: UUID, operator: ManagerDep, keys: KeysDep, session: SessionDep
) -> KeyOut:
    return await _key_action(key_id, "reset", operator, keys, session)


@router.post("/keys/{key_id}/block", response_model=KeyOut)
async def block_key(
    key_id: UUID, operator: ManagerDep, keys: KeysDep, session: SessionDep
) -> KeyOut:
    return await _key_action(key_id, "block", operator, keys, session)


@router.post("/keys/{key_id}/unblock", response_model=KeyOut)
async def unblock_key(
    key_id: UUID, operator: ManagerDep, keys: KeysDep, session: SessionDep
) -> KeyOut:
    return await _key_action(key_id, "unblock", operator, keys, session)


@router.delete("/keys/{key_id}")
async def delete_key(
    key_id: UUID, operator: ManagerDep, keys: KeysDep, session: SessionDep
) -> dict[str, bool]:
    key = await keys.get(key_id)
    if key.key_type == "user":
        raise Forbidden("用户 Key 只有本人能删除；要止血请停用")
    await keys.delete(key, actor=operator.uid)
    await session.commit()
    return {"deleted": True}


async def _visible_key(keys: KeyService, key_id: UUID, operator: Operator) -> McpApiKey:
    key = await keys.get(key_id)
    if not operator.can_manage and not (
        key.key_type == "user" and str(key.user_uuid) == operator.uid
    ):
        raise Forbidden("只能查看自己的 Key")
    return key


@router.get("/keys/{key_id}/grants", response_model=list[ServiceScopeOut])
async def key_grants(
    key_id: UUID,
    operator: OperatorDep,
    keys: KeysDep,
    registry: RegistryDep,
    grants: GrantsDep,
) -> list[ServiceScopeOut]:
    key = await _visible_key(keys, key_id, operator)
    services = {s.name: s for s in await registry.list()}
    return scopes_of(await grants.of_key(key, fresh=True), services)


@router.put("/keys/{key_id}/grants", response_model=list[ServiceScopeOut])
async def set_key_grants(
    key_id: UUID,
    body: KeyGrantsIn,
    operator: OperatorDep,
    keys: KeysDep,
    registry: RegistryDep,
    grants: GrantsDep,
    session: SessionDep,
) -> list[ServiceScopeOut]:
    """分配权限：只改列出来的服务；每个都要是自己能授权的。"""
    key = await keys.get(key_id)
    for item in body.grants:
        await _require_assign(grants, operator, item.service)
    for item in body.grants:
        svc = await registry.get(item.service)
        await grants.set_key(svc, key, scope=item.scope, tools=item.tools, operator=operator.uid)
    await session.commit()
    services = {s.name: s for s in await registry.list()}
    return scopes_of(await grants.of_key(key, fresh=True), services)
