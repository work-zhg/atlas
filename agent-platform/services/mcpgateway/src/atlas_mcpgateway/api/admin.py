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
★ 授权只认主体：授权列表里的主体由这里换成 Key（ApiKeys.by_subjects）。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from opentelemetry import trace

from ..access.authorization import Authorization, Operator
from ..access.key_access import KeyAccess
from ..catalog.service import ServiceCatalog
from ..db.models import McpApiKey, McpService
from ..errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ..invocation.rejection import Rejection
from ..keys.service import ApiKeys
from ..upstream import UpstreamUnavailable
from .deps import (
    AuthzDep,
    CatalogDep,
    InvocationDep,
    KeysDep,
    ManagerDep,
    OperatorDep,
    PolicyDep,
    SessionDep,
    SettingsDep,
    verified_tenant,
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

# ★ 先核对租户、进入它（deps.verified_tenant），之后的读写都按它自动过滤
router = APIRouter(prefix="/api/v1", tags=["admin"], dependencies=[Depends(verified_tenant)])
_tracer = trace.get_tracer("atlas_mcpgateway")


def _uid(operator: Operator) -> UUID | None:
    try:
        return UUID(operator.uid)
    except ValueError:
        return None


def _credential(body: CredentialIn) -> tuple[dict[str, Any], str | None]:
    raw = body.model_dump(exclude={"secret"})
    return raw, (body.secret or "").strip() or None


def _descriptor(raw: str | None) -> bytes | None:
    """上传的描述文件（base64）→ bytes。"""
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise Invalid("描述文件不是 base64 编码的 FileDescriptorSet") from exc


async def _safe_access(authz: Authorization, key: McpApiKey | None) -> KeyAccess | None:
    if key is None:
        return None
    try:
        return await authz.access_of(str(key.subject_uid))
    except Unavailable:
        return None


class _Viewer:
    """当前用户：是哪些服务的所有者、他的用户 Key 被授了什么。一个请求算一次。"""

    def __init__(self, operator: Operator, key: McpApiKey | None, access: KeyAccess | None) -> None:
        self.operator, self.key, self.access = operator, key, access

    @classmethod
    async def load(cls, operator: Operator, authz: Authorization, keys: ApiKeys) -> _Viewer:
        operator = await authz.with_ownership(operator)
        uid = _uid(operator)
        key = await keys.user_key(uid) if uid is not None else None
        return cls(operator, key, await _safe_access(authz, key))

    def owner(self, svc: McpService) -> bool:
        return self.operator.can_assign(svc.name)

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


# ───────────────────────────── 我


@router.get("/me")
async def me(
    operator: OperatorDep, authz: AuthzDep, keys: KeysDep, settings: SettingsDep
) -> dict[str, Any]:
    op = await authz.with_ownership(operator)
    uid = _uid(op)
    return {
        "uid": op.uid,
        "can_manage": op.can_manage,
        "owned_services": sorted(op.owned or ()),
        "owns_all": op.owns_all,
        "has_key": uid is not None and await keys.user_key(uid) is not None,
        "public_url": settings.public_url.rstrip("/") or None,
    }


# ───────────────────────────── 服务


@router.get("/services", response_model=list[ServiceOut])
async def list_services(
    operator: OperatorDep, catalog: CatalogDep, authz: AuthzDep, keys: KeysDep
) -> list[ServiceOut]:
    viewer = await _Viewer.load(operator, authz, keys)
    mine = [
        service_out(svc, await catalog.tools(svc), viewer.my(svc))
        for svc in await catalog.list()
        if viewer.sees(svc)
    ]
    # 平台服务（公开）：所有租户看得到、改不了
    platform = [
        service_out(svc, await catalog.tools(svc)) for svc in await catalog.platform_services()
    ]
    return mine + platform


@router.post("/discover", response_model=DiscoveredOut)
async def discover(body: DiscoverIn, operator: ManagerDep, catalog: CatalogDep) -> DiscoveredOut:
    raw, secret = _credential(body.credential)
    preview = await catalog.discover(
        kind=body.kind,
        connection=body.connection,
        credential=raw,
        secret=secret,
        descriptor=_descriptor(body.descriptor),
    )
    return discovered_out(preview)


@router.post("/services", response_model=ServiceDetailOut, status_code=201)
async def create_service(
    body: ServiceIn, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> ServiceDetailOut:
    raw, secret = _credential(body.credential)
    row = await catalog.register(
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
        descriptor=_descriptor(body.descriptor),
        actor=operator.uid,
    )
    await session.commit()
    return service_detail(
        row,
        await catalog.tools(row, include_removed=True),
        my=my_access(row, owner=True, access=None, has_key=True),
        can_manage=True,
        can_assign=True,
    )


async def _detail(
    name: str, operator: Operator, catalog: ServiceCatalog, authz: Authorization, keys: ApiKeys
) -> ServiceDetailOut:
    row = await catalog.get(name)
    viewer = await _Viewer.load(operator, authz, keys)
    if not viewer.sees(row):
        raise NotFound(f"MCP 服务 {name} 不存在", service=name)
    return service_detail(
        row,
        await catalog.tools(row, include_removed=True),
        my=viewer.my(row),
        can_manage=operator.can_manage,
        can_assign=viewer.owner(row),
    )


@router.get("/services/{name}", response_model=ServiceDetailOut)
async def get_service(
    name: str, operator: OperatorDep, catalog: CatalogDep, authz: AuthzDep, keys: KeysDep
) -> ServiceDetailOut:
    return await _detail(name, operator, catalog, authz, keys)


@router.patch("/services/{name}", response_model=ServiceDetailOut)
async def update_service(
    name: str,
    body: ServicePatch,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    changes = body.model_dump(exclude_unset=True)
    if "display_name" in changes and not (changes["display_name"] or "").strip():
        raise Invalid("显示名称不能为空")
    await catalog.update(name, changes, actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, catalog, authz, keys)


@router.put("/services/{name}/connection", response_model=ServiceDetailOut)
async def update_connection(
    name: str,
    body: ConnectionIn,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    await catalog.change_connection(
        name, body.connection, pass_identity=body.pass_identity, actor=operator.uid
    )
    await session.commit()
    return await _detail(name, operator, catalog, authz, keys)


@router.put("/services/{name}/credential", response_model=ServiceDetailOut)
async def update_credential(
    name: str,
    body: CredentialIn,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    raw, secret = _credential(body)
    await catalog.change_credential(name, raw, secret, actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, catalog, authz, keys)


async def _service_action(
    name: str,
    action: str,
    operator: Operator,
    catalog: ServiceCatalog,
    authz: Authorization,
    keys: ApiKeys,
    session: Any,
) -> ServiceDetailOut:
    if action == "refresh":
        await catalog.refresh(await catalog.get(name), manual=True)
    else:
        await catalog.set_enabled(name, action == "enable", actor=operator.uid)
    await session.commit()
    return await _detail(name, operator, catalog, authz, keys)


@router.post("/services/{name}/enable", response_model=ServiceDetailOut)
async def enable_service(
    name: str,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "enable", operator, catalog, authz, keys, session)


@router.post("/services/{name}/disable", response_model=ServiceDetailOut)
async def disable_service(
    name: str,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "disable", operator, catalog, authz, keys, session)


@router.post("/services/{name}/refresh", response_model=ServiceDetailOut)
async def refresh_service(
    name: str,
    operator: ManagerDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServiceDetailOut:
    return await _service_action(name, "refresh", operator, catalog, authz, keys, session)


@router.post("/services/{name}/test", response_model=TestOut)
async def test_service(name: str, operator: ManagerDep, catalog: CatalogDep) -> TestOut:
    """测试连接：按保存的连接和凭据发现一次工具（不写库）。"""
    row = await catalog.get(name)
    if row.kind == "rest" and not row.connection.get("spec_url"):
        try:
            status = await catalog.upstream.probe(row.connection["base_url"])
        except UpstreamUnavailable as exc:
            return TestOut(ok=False, detail=f"连不上：{exc}")
        return TestOut(ok=True, detail=f"能连上（HTTP {status}）")
    try:
        preview = await catalog.preview_of(row)
    except Invalid as exc:
        return TestOut(ok=False, detail=exc.message)
    found = preview.found
    count = len(preview.definitions or found.operations or found.methods)
    return TestOut(ok=True, detail=f"连接正常，发现 {count} 个工具")


@router.post("/services/{name}/rediscover", response_model=DiscoveredOut)
async def rediscover(
    name: str, body: RediscoverIn, operator: ManagerDep, catalog: CatalogDep
) -> DiscoveredOut:
    row = await catalog.get(name)
    if row.kind == "mcp":
        raise Invalid("MCP 代理的工具从上游拉取，用「立即刷新」")
    preview = await catalog.preview_of(row, descriptor=_descriptor(body.descriptor))
    existing = {t.name for t in await catalog.tools(row, include_removed=True)}
    return discovered_out(preview, existing=existing)


@router.post("/services/{name}/import", response_model=list[str])
async def import_tools(
    name: str, body: ImportIn, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> list[str]:
    added = await catalog.reimport(
        name, body.selected, descriptor=_descriptor(body.descriptor), actor=operator.uid
    )
    await session.commit()
    return added


# ───────────────────────────── 工具


@router.post("/services/{name}/tools", response_model=ToolOut, status_code=201)
async def add_tool(
    name: str, body: ToolIn, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> ToolOut:
    tool = await catalog.add_tool(
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
    catalog: CatalogDep,
    session: SessionDep,
) -> ToolOut:
    row = await catalog.edit_tool(
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
    name: str, tool: str, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> dict[str, bool]:
    await catalog.delete_tool(name, tool, actor=operator.uid)
    await session.commit()
    return {"deleted": True}


@router.post("/services/{name}/tools/{tool}/enable", response_model=ToolOut)
async def enable_tool(
    name: str, tool: str, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> ToolOut:
    row = await catalog.set_tool_status(name, tool, active=True, actor=operator.uid)
    await session.commit()
    return tool_out(name, row)


@router.post("/services/{name}/tools/{tool}/disable", response_model=ToolOut)
async def disable_tool(
    name: str, tool: str, operator: ManagerDep, catalog: CatalogDep, session: SessionDep
) -> ToolOut:
    row = await catalog.set_tool_status(name, tool, active=False, actor=operator.uid)
    await session.commit()
    return tool_out(name, row)


@router.post("/services/{name}/tools/{tool}/call", response_model=TryOut)
async def try_call(
    name: str,
    tool: str,
    body: TryIn,
    operator: OperatorDep,
    keys: KeysDep,
    invocation: InvocationDep,
) -> TryOut:
    """试调：和真实调用一样认 Key、判权、校验参数、调上游。会真的执行有副作用的工具。"""
    key = await keys.get(body.key_id)
    if not operator.can_manage and not (
        key.key_type == "user" and str(key.user_uuid) == operator.uid
    ):
        raise Forbidden("只能用自己的用户 Key 试调")
    with _tracer.start_as_current_span(f"try {name}/{tool}") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        try:
            caller = await invocation.admit(
                name, key.secret if key.status == "active" else "", user_uid=operator.uid
            )
            result = await invocation.execute(caller, tool, body.arguments)
        except Rejection as r:
            return TryOut(error=r.error(name), trace_id=trace_id)
    return TryOut(result=result, trace_id=trace_id)


# ───────────────────────────── 授权


@router.get("/services/{name}/grants", response_model=ServerGrantsOut)
async def list_grants(
    name: str, operator: OperatorDep, catalog: CatalogDep, authz: AuthzDep, keys: KeysDep
) -> ServerGrantsOut:
    svc = await catalog.get(name)
    await authz.require_assign(operator, name)
    found = await authz.grants_of(svc)
    by_subject = await keys.by_subjects(s.subject for s in found.subjects)
    granted = [(by_subject[s.subject], s) for s in found.subjects if s.subject in by_subject]
    granted.sort(key=lambda g: (g[0].key_type, g[0].name))
    return ServerGrantsOut(
        owners=[OwnerOut(user_id=e.subject_id, name=e.subject_name) for e in found.owners],
        keys=[key_grant_out(k, s.scope, list(s.tools)) for k, s in granted],
    )


@router.put("/services/{name}/grants/{key_id}", response_model=ServerGrantsOut)
async def set_grant(
    name: str,
    key_id: UUID,
    body: GrantIn,
    operator: OperatorDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await catalog.get(name)
    operator = await authz.require_assign(operator, name)
    key = await keys.get(key_id)
    await authz.set_scope(
        svc, str(key.subject_uid), scope=body.scope, tools=body.tools, operator=operator.uid
    )
    await session.commit()
    return await list_grants(name, operator, catalog, authz, keys)


@router.delete("/services/{name}/grants/{key_id}", response_model=ServerGrantsOut)
async def revoke_grant(
    name: str,
    key_id: UUID,
    operator: OperatorDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await catalog.get(name)
    operator = await authz.require_assign(operator, name)
    key = await keys.get(key_id)
    await authz.set_scope(svc, str(key.subject_uid), scope="none", tools=[], operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, catalog, authz, keys)


@router.post("/services/{name}/owners", response_model=ServerGrantsOut)
async def add_owner(
    name: str,
    body: OwnerIn,
    operator: OperatorDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await catalog.get(name)
    operator = await authz.require_assign(operator, name)
    await authz.add_owner(svc, str(body.user_id), operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, catalog, authz, keys)


@router.delete("/services/{name}/owners/{user_id}", response_model=ServerGrantsOut)
async def remove_owner(
    name: str,
    user_id: UUID,
    operator: OperatorDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    keys: KeysDep,
    session: SessionDep,
) -> ServerGrantsOut:
    svc = await catalog.get(name)
    operator = await authz.require_assign(operator, name)
    await authz.remove_owner(svc, str(user_id), operator=operator.uid)
    await session.commit()
    return await list_grants(name, operator, catalog, authz, keys)


@router.get("/users")
async def search_users(
    operator: OperatorDep, authz: AuthzDep, q: str = Query(min_length=1, max_length=50)
) -> list[dict[str, Any]]:
    await authz.require_grantor(operator)
    return [
        {"id": u.get("id"), "name": u.get("name"), "account": u.get("account")}
        for u in await authz.search_people(q)
    ]


@router.get("/key-options", response_model=list[KeyRefOut])
async def key_options(operator: OperatorDep, authz: AuthzDep, keys: KeysDep) -> list[KeyRefOut]:
    await authz.require_grantor(operator)
    return [key_ref(k) for k in await keys.list()]


# ───────────────────────────── API Key


async def _with_grants(
    rows: list[McpApiKey], catalog: ServiceCatalog, authz: Authorization
) -> list[KeyOut]:
    services = {s.name: s for s in await catalog.list()}
    found = await asyncio.gather(*(_safe_access(authz, k) for k in rows))
    return [key_out(k, scopes_of(a, services)) for k, a in zip(rows, found, strict=True)]


async def _my_key(keys: ApiKeys, operator: Operator) -> McpApiKey:
    uid = _uid(operator)
    key = await keys.user_key(uid) if uid is not None else None
    if key is None:
        raise NotFound("你还没有用户 Key")
    return key


@router.get("/keys/me", response_model=KeyOut | None)
async def my_key(
    operator: OperatorDep, keys: KeysDep, catalog: CatalogDep, authz: AuthzDep
) -> KeyOut | None:
    uid = _uid(operator)
    key = await keys.user_key(uid) if uid is not None else None
    if key is None:
        return None
    return (await _with_grants([key], catalog, authz))[0]


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
    catalog: CatalogDep,
    authz: AuthzDep,
    key_type: str | None = Query(default=None, pattern="^(user|client)$"),
    status: str | None = Query(default=None, pattern="^(active|blocked)$"),
    q: str | None = None,
) -> list[KeyOut]:
    rows = await keys.list(key_type=key_type, status=status, q=q)
    return await _with_grants(rows, catalog, authz)


@router.get("/client-types", response_model=list[ClientTypeOut])
async def client_types(_: ManagerDep, policy: PolicyDep) -> list[ClientTypeOut]:
    return [ClientTypeOut(code=c, apps=list(apps)) for c, apps in policy.items()]


@router.post("/client-keys", response_model=KeyOut, status_code=201)
async def create_client_key(
    body: ClientKeyIn,
    operator: ManagerDep,
    keys: KeysDep,
    policy: PolicyDep,
    session: SessionDep,
) -> KeyOut:
    if not policy.known(body.client_type):
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
    key_id: UUID, action: str, operator: Operator, keys: ApiKeys, session: Any
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


async def _visible_key(keys: ApiKeys, key_id: UUID, operator: Operator) -> McpApiKey:
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
    catalog: CatalogDep,
    authz: AuthzDep,
) -> list[ServiceScopeOut]:
    key = await _visible_key(keys, key_id, operator)
    services = {s.name: s for s in await catalog.list()}
    return scopes_of(await authz.access_of(str(key.subject_uid), fresh=True), services)


@router.put("/keys/{key_id}/grants", response_model=list[ServiceScopeOut])
async def set_key_grants(
    key_id: UUID,
    body: KeyGrantsIn,
    operator: OperatorDep,
    keys: KeysDep,
    catalog: CatalogDep,
    authz: AuthzDep,
    session: SessionDep,
) -> list[ServiceScopeOut]:
    """分配权限：只改列出来的服务；每个都要是自己能授权的。"""
    key = await keys.get(key_id)
    for item in body.grants:
        operator = await authz.require_assign(operator, item.service)
    subject = str(key.subject_uid)
    for item in body.grants:
        svc = await catalog.get(item.service)
        await authz.set_scope(
            svc, subject, scope=item.scope, tools=item.tools, operator=operator.uid
        )
    await session.commit()
    services = {s.name: s for s in await catalog.list()}
    return scopes_of(await authz.access_of(subject, fresh=True), services)
