"""运营接口（/ops/v1）：技能市场，以及租户的创建、删除。只给运营中心（先验签名）。

    GET   /ops/v1/market/skills                              市场里的技能和版本
    POST  /ops/v1/market/skills                              上传一个版本（zip），直接发布
                                                             （表单 install_by_default 顺带改默认）
    PATCH /ops/v1/market/skills/{slug}                       改「默认安装」
    POST  /ops/v1/market/skills/{slug}/versions/{v}/revoke   下架（不能再装；装走的不动）
    PUT    /ops/v1/tenants/{tenant}                          创建（运营接口库给的）：装市场技能，
                                                             参数见 SkillTenantLifecycle.create
    DELETE /ops/v1/tenants/{tenant}                          删除（运营接口库给的）

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

from atlas_opsapi import PREFIX, OperatorStamp, OpsGuard, OpsRoutes, Rejected, TenantLifecycle
from atlas_tenancy import PurgeReport
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..check.package import ScanBlocked, load_zip
from ..db.models import Base, Skill, SkillAudit, SkillVersion
from ..db.session import get_sessionmaker
from ..errors import Invalid
from ..market.service import MarketService
from ..schemas import MarketSkillOut, MarketVersionOut
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


def _storage(app: FastAPI, settings: SkillSettings) -> Any:
    storage = getattr(app.state, "storage", None)
    if storage is None:
        storage = make_storage(settings)
        app.state.storage = storage
    return storage


class SkillTenantLifecycle(TenantLifecycle):
    """技能应用的租户接入。

    创建：从技能市场给新租户装技能 —— 装进租户以后就是这个租户自己的技能（market/service.py）。
      参数（开通时运营中心给技能应用填的）：
        install_defaults  装全部「默认安装」的市场技能，默认是
        skills            只装这些（slug 或 slug@版本），给了就不看 install_defaults
      能重做：已经装过的版本不再装一遍。
    删除：先删对象存储里这个租户的包，再分批删数据库里的行（基类）。
    """

    purge_order = (SkillAudit, SkillVersion, Skill)

    def __init__(self, app: FastAPI) -> None:
        super().__init__(get_sessionmaker(), base=Base)
        self._app = app

    async def create(self, tenant_uuid: UUID, params: dict[str, Any]) -> None:
        skills = params.get("skills")
        install_defaults = params.get("install_defaults", True)
        if skills is not None and (
            not isinstance(skills, list) or not all(isinstance(x, str) for x in skills)
        ):
            raise Rejected("skills 必须是字符串列表（slug 或 slug@版本）")
        if not isinstance(install_defaults, bool):
            raise Rejected("install_defaults 必须是 true / false")
        if not skills and not install_defaults:
            return
        settings = get_settings()
        async with self._sessions() as session:
            service = MarketService(
                session=session, storage=_storage(self._app, settings), settings=settings
            )
            # 空列表 = 装全部默认安装的
            await service.install_many(skills or [], actor="运营中心（开通租户）")
            await session.commit()

    async def delete_external(self, tenant_uuid: UUID, budget: int) -> PurgeReport:
        report = PurgeReport()
        try:
            storage = _storage(self._app, get_settings())
        except StorageNotConfigured:
            return report  # 没配对象存储：没有包要删
        async with self._sessions() as session:
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
    app.state.ops_guard = guard = OpsGuard(key)
    OpsRoutes.mount(app, guard, SkillTenantLifecycle(app))
    app.include_router(router)
