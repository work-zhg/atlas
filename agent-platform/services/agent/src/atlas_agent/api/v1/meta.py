from __future__ import annotations

from fastapi import APIRouter

from ...deps import MetaServiceDep
from ...schemas.meta import ModelListResponse, ToolListResponse

router = APIRouter(tags=["meta"])


@router.get("/models", response_model=ModelListResponse)
async def list_models(service: MetaServiceDep) -> ModelListResponse:
    """model_catalog ⋈ 网关 /v1/models 巡检结果。"""
    return await service.list_models()


@router.get("/tools", response_model=ToolListResponse)
async def list_tools(service: MetaServiceDep) -> ToolListResponse:
    """内置工具 + 已接入 MCP 工具。bash 返回 available=false（决策 2）。"""
    return await service.list_tools()
