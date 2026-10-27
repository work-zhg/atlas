"""给 Atlas 的内部接口（MCP 方案设计 §4.1、§5.4、§6.1）。网关（nginx）一律 404，内网免令牌。

    GET  /internal/metadata                 服务、工具定义、复核结论（Atlas 缓存，装配时不连网）
    GET  /internal/subjects/{uid}/access    主体此刻能用哪些服务 + 主体的 Key（一次给出，demo F5）
    POST /internal/services/{name}/refresh  立即重新拉取（Atlas 的 MCP 页面「刷新」）

★ 「能用哪些服务」和数据面判权用同一份缓存：两道闸门的判断依据一致（§5.4）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from ..errors import Invalid
from .deps import KeysDep, RegistryDep, UsableDep
from .schemas import MetadataOut, MetadataServiceOut, SubjectAccessOut
from .views import metadata_out

router = APIRouter(prefix="/internal", tags=["internal"])


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


@router.get("/subjects/{uid}/access", response_model=SubjectAccessOut)
async def subject_access(
    uid: str, registry: RegistryDep, keys: KeysDep, usable: UsableDep
) -> SubjectAccessOut:
    try:
        subject = UUID(uid)
    except ValueError as exc:
        raise Invalid("主体必须是用户中心的用户 id") from exc
    allowed = await usable.usable(str(subject))
    services = sorted(
        s.name for s in await registry.list() if s.status == "enabled" and allowed.allows(s.name)
    )
    key = await keys.of_subject(subject)
    return SubjectAccessOut(
        subject=str(subject),
        services=services,
        all_services=allowed.all_services,
        key=key.api_key if key is not None and key.status == "active" else None,
        key_status=key.status if key is not None else "none",  # type: ignore[arg-type]
    )


@router.post("/services/{name}/refresh", response_model=MetadataServiceOut)
async def refresh(name: str, registry: RegistryDep) -> MetadataServiceOut:
    svc = await registry.refresh(await registry.get(name), manual=True)
    return metadata_out(svc, await registry.tools(svc, include_removed=True))
