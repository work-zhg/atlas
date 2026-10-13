"""运行时用的只读接口（设计 §13.1）。

★ 只返回 published / disabled / revoked。待审查与被拒的上传在这里**不存在** ——
  运行时拿不到草稿，不是靠它自觉过滤，而是接口根本不给。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_internal
from ..schemas import McpServerOut, RevokedSkill, SkillCatalogItem, SkillVersionOut, ToolReviewOut
from .deps import RegistryDep, SkillServiceDep

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal)])


@router.get("/skills/catalog", response_model=list[SkillCatalogItem])
async def catalog(service: SkillServiceDep) -> list[SkillCatalogItem]:
    return await service.catalog()


@router.get("/skills/revoked", response_model=list[RevokedSkill])
async def revoked(service: SkillServiceDep) -> list[RevokedSkill]:
    return await service.revoked()


@router.get("/skills/{slug}/versions", response_model=list[SkillVersionOut])
async def versions(slug: str, service: SkillServiceDep) -> list[SkillVersionOut]:
    return await service.runtime_versions(slug)


@router.get("/skills/{slug}/versions/{version}", response_model=SkillVersionOut)
async def version(slug: str, version: int, service: SkillServiceDep) -> SkillVersionOut:
    return await service.runtime_version(slug, version)


@router.get("/mcp/servers", response_model=list[McpServerOut])
async def mcp_servers(registry: RegistryDep) -> list[McpServerOut]:
    return await registry.list(enabled_only=True)


@router.get("/mcp/reviews", response_model=list[ToolReviewOut])
async def mcp_reviews(server: str, registry: RegistryDep) -> list[ToolReviewOut]:
    return await registry.reviews(server)
