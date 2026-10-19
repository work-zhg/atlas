"""技能 / MCP 的只读视图（技能 / MCP 设计 §13.3）。

技能的内容、审查、发布在配置服务（atlas-config 的 /config/*）；这里只有运行时
才知道的那一半：被谁引用、用得怎样、MCP server 此刻的工具定义。

★ 角色检查（builder+ / 刷新 admin）等用户模块上线后加：现在的身份只有 dev 模式
  （identity.py），没有角色可判。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...deps import CatalogServiceDep
from ...schemas.catalog import (
    McpServerDetailOut,
    McpServerListOut,
    SkillListOut,
    SkillReferencesOut,
    SkillUsageOut,
)

router = APIRouter(tags=["catalog"])


@router.get("/skills", response_model=SkillListOut)
async def list_skills(service: CatalogServiceDep) -> SkillListOut:
    """技能目录（来自配置服务，经缓存）+ 引用数 + 近 7 天加载次数。编辑器选择器用。"""
    return await service.list_skills()


@router.get("/skills/usage", response_model=SkillUsageOut)
async def skill_usage(
    service: CatalogServiceDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> SkillUsageOut:
    return await service.skill_usage(days)


@router.get("/skills/{slug}/references", response_model=SkillReferencesOut)
async def skill_references(slug: str, service: CatalogServiceDep) -> SkillReferencesOut:
    """引用该技能的 agent（按当前版本）—— 停用 / 下架前必查。"""
    return await service.skill_references(slug)


@router.get("/mcp/servers", response_model=McpServerListOut)
async def list_mcp_servers(service: CatalogServiceDep) -> McpServerListOut:
    """★ 只读快照缓存，不触发发现：管理页不能被一个慢 server 拖住。"""
    return await service.list_mcp_servers()


@router.get("/mcp/servers/{name}", response_model=McpServerDetailOut)
async def get_mcp_server(name: str, service: CatalogServiceDep) -> McpServerDetailOut:
    return await service.mcp_server(name)


@router.post("/mcp/servers/{name}/refresh", response_model=McpServerDetailOut)
async def refresh_mcp_server(name: str, service: CatalogServiceDep) -> McpServerDetailOut:
    """强制重新发现。同一 server 10 秒内只执行一次。"""
    return await service.refresh_mcp_server(name)
