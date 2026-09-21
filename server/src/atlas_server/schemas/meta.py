from __future__ import annotations

from pydantic import BaseModel, Field


class ModelInfo(BaseModel):
    model: str
    provider: str
    display_name: str
    context_window: int
    max_output_tokens: int
    supports_thinking: bool
    # 与 supports_thinking 不同：haiku-4-5 支持思考但不支持 adaptive 模式
    supports_adaptive_thinking: bool
    # false 时编辑器不能显示 effort 选择器（传了就 400）
    supports_effort: bool
    supports_cache: bool
    # ★ 前端据此决定编辑器渲染 temperature 滑块还是 effort 选择器
    supports_temperature: bool
    min_cacheable_tokens: int
    is_available: bool = Field(description="网关巡检结果；false 时编辑器标灰不可选")


class ModelListResponse(BaseModel):
    data: list[ModelInfo]
    gateway_reachable: bool = Field(
        description="false 表示巡检失败，is_available 退化为 catalog 中的存量值"
    )


class ToolInfo(BaseModel):
    name: str
    kind: str  # builtin | mcp
    display_name: str
    description: str
    available: bool
    note: str | None = None
    #: 模型侧实际出现的工具名。require_approval_for 匹配的是这些，
    #: 不是 name —— bash 的模型侧名是 execute，filesystem 展开成 7 个。
    model_tool_names: list[str] = []


class ToolListResponse(BaseModel):
    data: list[ToolInfo]
