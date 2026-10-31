"""给应用（Atlas）的内部接口。网关（nginx）一律 404，内网免令牌。

    GET    /internal/metadata                    服务、工具定义（Atlas 缓存，装配时不连网）
    POST   /internal/client-keys                 申请智能体 Key（按类型 + 编码幂等）
    GET    /internal/client-keys?client_type&client_code
                          状态、Key 原文（正常时）、这把 Key 此刻能用的服务和工具
    PATCH  /internal/client-keys                 改名（智能体改名时同步）
    DELETE /internal/client-keys?client_type&client_code
                                                 删除（智能体删除时）：用户中心里的主体一并停用

★ 只有客户端类型的所属应用能调（settings.client_types，按请求头 X-Caller-App）。
★ 「能用的服务和工具」和数据面判权用同一份缓存：编辑器里看到的和运行时一致。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query

from ..auth import use_outbound
from ..dataplane import callable_tool
from ..db.models import McpApiKey
from ..errors import Forbidden, Invalid, NotFound
from ..keys import KeyService
from ..settings import GatewaySettings
from .deps import AccessDep, KeysDep, RegistryDep, SessionDep, SettingsDep
from .schemas import (
    InternalClientKeyIn,
    InternalClientKeyOut,
    MetadataOut,
    UsableServiceOut,
)
from .views import metadata_out

router = APIRouter(prefix="/internal", tags=["internal"])

#: 缺了按「未知应用」处理（403），不回 422
CallerApp = Annotated[str, Header(alias="X-Caller-App")]


@router.get("/metadata", response_model=MetadataOut)
async def metadata(
    registry: RegistryDep,
    names: str | None = Query(default=None, description="逗号分隔；缺省 = 全部"),
) -> MetadataOut:
    wanted = {n.strip() for n in names.split(",") if n.strip()} if names else None
    out = []
    for svc in await registry.list():
        if wanted is not None and svc.name not in wanted:
            continue
        out.append(metadata_out(svc, await registry.tools(svc, include_removed=True)))
    return MetadataOut(services=out)


def _require_app(settings: GatewaySettings, client_type: str, caller: str) -> None:
    apps = settings.client_types.get(client_type)
    if apps is None:
        raise NotFound(f"没有登记的客户端类型：{client_type}", code="CLIENT_TYPE_UNKNOWN")
    if caller not in apps:
        raise Forbidden(
            f"应用 {caller or '（未知）'} 不能操作客户端类型 {client_type}",
            code="CLIENT_TYPE_FORBIDDEN",
        )


async def _view(key: McpApiKey, registry: RegistryDep, access: AccessDep) -> InternalClientKeyOut:
    base = {
        "client_type": str(key.client_type),
        "client_code": str(key.client_code),
        "name": key.name,
        "key_uid": key.subject_uid,
    }
    if key.status != "active":
        return InternalClientKeyOut(**base, status=key.status)
    # ★ 和数据面一样：查 Key 的权限时 X-Auth-Uid = Key 的主体。
    #   用户中心不可用、缓存又过期时抛 Unavailable（503）：Atlas 当作这一轮 MCP 不可用
    subject = str(key.subject_uid)
    use_outbound({"X-Uid": subject, "X-Auth-Uid": subject})
    granted = await access.get(subject)
    servers: list[UsableServiceOut] = []
    for svc in await registry.list():
        if svc.status != "enabled":
            continue
        tools = [t.name for t in await registry.tools(svc) if callable_tool(t)]
        if svc.visibility == "private":
            if not granted.touches(svc.name):
                continue
            tools = [t for t in tools if granted.allows(svc.name, t)]
        # ★ 工具可以是空的（都被停用了）：服务照样列出，Atlas 据此说「工具不可用」而不是「没授权」
        servers.append(UsableServiceOut(name=svc.name, tools=tools))
    return InternalClientKeyOut(**base, status="active", key=key.secret, servers=servers)


async def _find(keys: KeyService, client_type: str, client_code: str) -> McpApiKey:
    key = await keys.client_key(client_type, client_code)
    if key is None:
        raise NotFound(
            f"客户端 {client_type}:{client_code} 还没有 Key", code="CLIENT_KEY_NOT_FOUND"
        )
    return key


@router.post("/client-keys", response_model=InternalClientKeyOut)
async def create_client_key(
    body: InternalClientKeyIn,
    keys: KeysDep,
    registry: RegistryDep,
    access: AccessDep,
    settings: SettingsDep,
    session: SessionDep,
    caller: CallerApp = "",
) -> InternalClientKeyOut:
    _require_app(settings, body.client_type, caller)
    found = await keys.client_key(body.client_type, body.client_code)
    if found is None and body.owner_uid is None:
        raise Invalid("创建时要给 owner_uid（用户中心主体的负责人）")
    key, _ = await keys.ensure_client(
        client_type=body.client_type,
        client_code=body.client_code,
        name=body.name.strip(),
        owner=str(body.owner_uid),
        actor=f"app:{caller}",
    )
    await session.commit()
    return await _view(key, registry, access)


@router.get("/client-keys", response_model=InternalClientKeyOut)
async def get_client_key(
    keys: KeysDep,
    registry: RegistryDep,
    access: AccessDep,
    settings: SettingsDep,
    client_type: str = Query(min_length=1),
    client_code: str = Query(min_length=1),
    caller: CallerApp = "",
) -> InternalClientKeyOut:
    _require_app(settings, client_type, caller)
    return await _view(await _find(keys, client_type, client_code), registry, access)


@router.patch("/client-keys", response_model=InternalClientKeyOut)
async def rename_client_key(
    body: InternalClientKeyIn,
    keys: KeysDep,
    registry: RegistryDep,
    access: AccessDep,
    settings: SettingsDep,
    session: SessionDep,
    caller: CallerApp = "",
) -> InternalClientKeyOut:
    _require_app(settings, body.client_type, caller)
    key = await _find(keys, body.client_type, body.client_code)
    await keys.rename(key, body.name.strip())
    await session.commit()
    return await _view(key, registry, access)


@router.delete("/client-keys")
async def delete_client_key(
    keys: KeysDep,
    settings: SettingsDep,
    session: SessionDep,
    client_type: str = Query(min_length=1),
    client_code: str = Query(min_length=1),
    caller: CallerApp = "",
) -> dict[str, bool]:
    _require_app(settings, client_type, caller)
    key = await keys.client_key(client_type, client_code)
    if key is None:
        return {"deleted": False}
    await keys.delete(key, actor=f"app:{caller}")
    await session.commit()
    return {"deleted": True}
