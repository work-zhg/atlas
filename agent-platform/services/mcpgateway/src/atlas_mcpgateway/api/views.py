"""库里的行 → 接口里的形状。"""

from __future__ import annotations

from ..dataplane import callable_tool
from ..db.models import McpApiKey, McpService, McpTool
from ..definitions import content_hash, model_name, read_only
from ..grants import KeyGrant
from ..keys import mask
from ..perms import KeyAccess
from ..upstreams import grpc as grpc_up
from ..upstreams import rest as rest_up
from ..upstreams.credentials import hint
from ..upstreams.hub import Discovered
from .schemas import (
    CredentialOut,
    DiscoveredOut,
    DiscoveredToolOut,
    KeyGrantOut,
    KeyOut,
    KeyRefOut,
    MetadataServiceOut,
    MetadataToolOut,
    MyAccessOut,
    ServiceDetailOut,
    ServiceOut,
    ServiceScopeOut,
    ToolOut,
)

#: 停用的工具在给 Atlas 的元数据里以这条 issue 出现：Atlas 按「有 issue = 不可用」处理
DISABLED_ISSUE = "MCP 管理员已停用这个工具"


def credential_out(svc: McpService) -> CredentialOut:
    data = dict(svc.credential or {})
    return CredentialOut(
        type=str(data.pop("type", "none")),
        **{k: v for k, v in data.items() if k in CredentialOut.model_fields},
        hint=hint(svc.credential_secret),
    )


def my_access(
    svc: McpService, *, owner: bool, access: KeyAccess | None, has_key: bool
) -> MyAccessOut:
    if svc.visibility == "public":
        return MyAccessOut(owner=owner, scope="public")
    if not has_key:
        return MyAccessOut(owner=owner, scope="no_key")
    if access is None:
        return MyAccessOut(owner=owner, scope="none")
    if svc.name in access.servers:
        return MyAccessOut(owner=owner, scope="all")
    tools = sorted(access.tools_of(svc.name))
    return MyAccessOut(owner=owner, scope="tools" if tools else "none", tools=tools)


def service_out(svc: McpService, tools: list[McpTool], my: MyAccessOut | None = None) -> ServiceOut:
    current = [t for t in tools if t.removed_at is None]
    return ServiceOut(
        name=svc.name,
        display_name=svc.display_name,
        description=svc.description,
        kind=svc.kind,
        visibility=svc.visibility,
        enabled=svc.status == "enabled",
        connection=dict(svc.connection or {}),
        credential=credential_out(svc),
        pass_identity=svc.pass_identity,
        created_by=svc.created_by,
        created_at=svc.created_at,
        updated_at=svc.updated_at,
        fetched_at=svc.fetched_at,
        fetch_error=svc.fetch_error,
        changed_at=svc.changed_at,
        tool_count=len(current),
        active_tool_count=sum(1 for t in current if callable_tool(t)),
        my=my or MyAccessOut(),
    )


def tool_out(service: str, t: McpTool) -> ToolOut:
    return ToolOut(
        name=t.name,
        model_name=model_name(service, t.name),
        description=t.description,
        input_schema=t.input_schema,
        annotations=t.annotations,
        effect=t.effect,
        mapping=t.mapping,
        status=t.status,
        issues=list(t.issues),
        digest=t.digest,
        changed_at=t.changed_at,
        previous=t.previous,
        callable=callable_tool(t),
    )


def service_detail(
    svc: McpService,
    tools: list[McpTool],
    *,
    my: MyAccessOut,
    can_manage: bool,
    can_assign: bool,
) -> ServiceDetailOut:
    current = [t for t in tools if t.removed_at is None]
    return ServiceDetailOut(
        **service_out(svc, tools, my).model_dump(),
        tools=[tool_out(svc.name, t) for t in current],
        removed_tools=sorted(t.name for t in tools if t.removed_at is not None),
        can_manage=can_manage,
        can_assign=can_assign,
    )


def discovered_out(found: Discovered, *, existing: set[str] | None = None) -> DiscoveredOut:
    existing = existing or set()
    items: list[DiscoveredToolOut] = []
    if found.kind == "mcp":
        items = [
            DiscoveredToolOut(
                key=d.name,
                name=d.name,
                label=d.name,
                description=d.description,
                effect=d.effect,
                input_schema=d.input_schema,
                issues=list(d.issues),
                exists=d.name in existing,
            )
            for d in found.definitions
        ]
    elif found.kind == "rest":
        items = [_operation_out(o, existing) for o in found.operations]
    else:
        items = [_method_out(m, existing) for m in found.methods]
    return DiscoveredOut(kind=found.kind, tools=items, services=found.services)


def _operation_out(o: rest_up.Operation, existing: set[str]) -> DiscoveredToolOut:
    from ..definitions import schema_from_params

    return DiscoveredToolOut(
        key=o.key,
        name=o.name,
        label=o.key,
        description=o.description,
        effect=o.effect,
        input_schema=schema_from_params(o.params),
        unsupported=o.unsupported,
        exists=o.name in existing,
    )


def _method_out(m: grpc_up.Method, existing: set[str]) -> DiscoveredToolOut:
    return DiscoveredToolOut(
        key=m.key,
        name=m.name,
        label=m.key,
        description=f"gRPC {m.key}",
        effect=m.effect,
        input_schema=m.input_schema,
        stream=m.stream,
        unsupported=m.unsupported,
        exists=m.name in existing,
    )


def key_ref(k: McpApiKey) -> KeyRefOut:
    return KeyRefOut(
        id=k.uuid,
        key_type=k.key_type,
        name=k.name,
        status=k.status,
        subject_uid=k.subject_uid,
        client_code=k.client_code,
    )


def key_grant_out(g: KeyGrant) -> KeyGrantOut:
    return KeyGrantOut(key=key_ref(g.key), scope=g.scope, tools=sorted(g.tools))


def scopes_of(access: KeyAccess | None, services: dict[str, McpService]) -> list[ServiceScopeOut]:
    if access is None:
        return []
    out: list[ServiceScopeOut] = []
    for name, svc in sorted(services.items()):
        if svc.visibility != "private":
            continue
        if name in access.servers:
            out.append(ServiceScopeOut(service=name, display_name=svc.display_name, scope="all"))
        elif tools := sorted(access.tools_of(name)):
            out.append(
                ServiceScopeOut(
                    service=name, display_name=svc.display_name, scope="tools", tools=tools
                )
            )
    return out


def key_out(k: McpApiKey, grants: list[ServiceScopeOut] | None = None) -> KeyOut:
    return KeyOut(
        id=k.uuid,
        key_type=k.key_type,
        user_uid=k.user_uuid,
        client_type=k.client_type,
        client_code=k.client_code,
        name=k.name,
        subject_uid=k.subject_uid,
        masked=mask(k.secret),
        status=k.status,
        created_by=k.created_by,
        created_at=k.created_at,
        rotated_at=k.rotated_at,
        grants=grants or [],
    )


def metadata_out(svc: McpService, tools: list[McpTool]) -> MetadataServiceOut:
    current = [t for t in tools if t.removed_at is None]
    out_tools = []
    for t in current:
        issues = list(t.issues) + ([DISABLED_ISSUE] if t.status == "disabled" else [])
        out_tools.append(
            MetadataToolOut(
                name=t.name,
                model_name=model_name(svc.name, t.name),
                description=t.description,
                input_schema=t.input_schema,
                annotations=t.annotations,
                read_only=read_only(t.annotations),
                effect=t.effect,
                digest=t.digest,
                issues=issues,
                status="invalid" if issues else "ok",
                changed_at=t.changed_at,
                previous=t.previous,
            )
        )
    return MetadataServiceOut(
        name=svc.name,
        display_name=svc.display_name,
        description=svc.description,
        kind=svc.kind,
        visibility=svc.visibility,
        enabled=svc.status == "enabled",
        fetched_at=svc.fetched_at,
        fetch_error=svc.fetch_error,
        changed_at=svc.changed_at,
        content_hash=svc.content_hash or content_hash(t.digest for t in current),
        tools=out_tools,
        removed_tools=sorted(t.name for t in tools if t.removed_at is not None),
    )
