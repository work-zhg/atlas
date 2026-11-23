"""运营接口（/ops/v1）：技能市场，以及租户的停用、恢复、删除。只给运营中心（先验签名）。

    GET   /ops/v1/market/skills                              市场里的技能和版本
    POST  /ops/v1/market/skills                              上传一个版本（zip），直接发布
                                                             （表单 install_by_default 顺带改默认）
    PATCH /ops/v1/market/skills/{slug}                       改「默认安装」
    POST  /ops/v1/market/skills/{slug}/versions/{v}/revoke   下架（不能再装；装走的不动）
    POST  /ops/v1/tenants/{tenant}/market-skills             给租户装：{skills: [slug@版本…]}，
                                                             空 = 装全部默认安装的（开通租户时调）
    POST  /ops/v1/tenants/{tenant}/suspend|resume|purge      运营接口库给的

★ 市场是产品自带的技能，不属于任何租户；装进租户以后才是租户的（market/service.py）。
★ 写操作在返回前提交。
★ 删除租户：先删对象存储里这个租户的包（{租户}/_skills/、{租户}/_skills_drafts/），再分批删
  数据库里的行。多租户之前发布的在桶根 _skills/ 下、按 storage_key 逐个版本删。
  ★ 只删自己的子目录，不删整个 {租户}/：桶和 Atlas 的工作空间、知识库共用。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from uuid import UUID

from atlas_opsapi import (
    PREFIX,
    OperatorStamp,
    OpsGuard,
    OpsRoutes,
    TenantGate,
    TenantLifecycleService,
)
from atlas_tenancy import PurgeReport, TenantDataPurge, TenantScope
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..check.package import ScanBlocked, load_zip
from ..db.models import Base, Skill, SkillAudit, SkillVersion
from ..db.session import get_sessionmaker
from ..errors import Invalid
from ..market.service import MarketService
from ..schemas import MarketInstalled, MarketInstallIn, MarketSkillOut, MarketVersionOut
from ..settings import SkillSettings, get_settings
from ..store.layout import DRAFTS_PREFIX, PUBLISHED_PREFIX
from ..store.objects import StorageNotConfigured, make_storage
from ..versions.service import limits_of
from .deps import MarketServiceDep

__all__ = ["mount_ops"]

router = APIRouter(prefix=PREFIX, tags=["ops"])


async def _operator(request: Request) -> OperatorStamp:
    return await request.app.state.ops_guard.verify(request)  # type: ignore[no-any-return]


Operator = Annotated[OperatorStamp, Depends(_operator)]


class DefaultIn(BaseModel):
    install_by_default: bool


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


@router.get("/market/skills", response_model=list[MarketSkillOut])
async def market(_: Operator, service: MarketServiceDep) -> list[MarketSkillOut]:
    return await service.listing()


@router.post("/market/skills", response_model=MarketVersionOut, status_code=201)
async def publish(op: Operator, request: Request, service: MarketServiceDep) -> MarketVersionOut:
    """表单：file（技能包 zip）、install_by_default（可选，true/false）。

    ★ 表单自己解析，不声明 File/Form 参数：FastAPI 会在依赖之前先把表单读完，验签时就拿不到原始
      请求体了（Stream consumed）。验签读过的请求体有缓存，之后再解析表单没问题。
    """
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, StarletteUploadFile):
        raise Invalid("缺少技能包（表单字段 file）")
    raw_default = form.get("install_by_default")
    install_by_default = None if raw_default is None else str(raw_default).lower() == "true"
    limits = limits_of(service.settings)
    data = await file.read(limits.max_bytes + 1)
    if len(data) > limits.max_bytes:
        raise ScanBlocked("archive", [f"上传的 zip 超过 {limits.max_bytes} 字节"])
    row = await service.publish(
        load_zip(data, limits), actor=op.who, install_by_default=install_by_default
    )
    await service.session.commit()
    return MarketVersionOut.model_validate(row, from_attributes=True)


@router.patch("/market/skills/{slug}")
async def set_default(
    slug: str, body: DefaultIn, _: Operator, service: MarketServiceDep
) -> dict[str, Any]:
    skill = await service.set_default(slug, body.install_by_default)
    await service.session.commit()
    return {"slug": skill.slug, "install_by_default": skill.install_by_default}


@router.post("/market/skills/{slug}/versions/{version}/revoke", response_model=MarketVersionOut)
async def revoke(
    slug: str, version: int, body: RevokeIn, _: Operator, service: MarketServiceDep
) -> MarketVersionOut:
    row = await service.revoke(slug, version, reason=body.reason)
    await service.session.commit()
    return MarketVersionOut.model_validate(row, from_attributes=True)


@router.post("/tenants/{tenant_uuid}/market-skills", response_model=list[MarketInstalled])
async def install_for_tenant(
    tenant_uuid: UUID, body: MarketInstallIn, op: Operator, request: Request
) -> list[MarketInstalled]:
    """开通租户时运营中心调（幂等）：装进这个租户的，就是这个租户自己的技能了。"""
    settings = get_settings()
    storage = _storage(request, settings)
    with TenantScope.enter(tenant_uuid):
        async with get_sessionmaker()() as session:
            service = MarketService(session=session, storage=storage, settings=settings)
            out = await service.install_many(body.skills, actor=op.who)
            await session.commit()
    return out


def _storage(request: Request, settings: SkillSettings) -> Any:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        storage = make_storage(settings)
        request.app.state.storage = storage
    return storage


class SkillTenantLifecycle:
    """技能应用的租户生命周期：没有后台工作要停；删除时先删对象存储里的包。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def on_suspend(self, tenant_uuid: UUID) -> None:
        return None

    async def on_resume(self, tenant_uuid: UUID) -> None:
        return None

    async def purge_external(self, tenant_uuid: UUID, limit: int) -> PurgeReport:
        report = PurgeReport()
        try:
            storage = getattr(self._app.state, "storage", None) or make_storage(get_settings())
        except StorageNotConfigured:
            return report  # 没配对象存储：没有包要删
        async with get_sessionmaker()() as session:
            legacy = [
                key
                for key in await session.scalars(select(SkillVersion.storage_key))
                if not key.startswith(f"{tenant_uuid}/")
            ]
        prefixes = [
            f"{tenant_uuid}/{PUBLISHED_PREFIX}/",
            f"{tenant_uuid}/{DRAFTS_PREFIX}/",
            *legacy,
        ]

        def _delete() -> int:
            return sum(storage.delete_tree(prefix) for prefix in prefixes)

        deleted = await asyncio.to_thread(_delete)
        if deleted:
            report.deleted["object"] = deleted
        return report


def mount_ops(app: FastAPI) -> None:
    settings = get_settings()
    key = settings.ops_api_key.get_secret_value() if settings.ops_api_key else None
    sessions = get_sessionmaker()
    app.state.ops_guard = guard = OpsGuard(key)
    app.state.tenant_gate = gate = TenantGate(sessions)
    purge = TenantDataPurge((SkillAudit, SkillVersion, Skill), base=Base)
    lifecycle = TenantLifecycleService(sessions, SkillTenantLifecycle(app), gate, purge)
    app.include_router(OpsRoutes(guard, lifecycle).router)
    app.include_router(router)
