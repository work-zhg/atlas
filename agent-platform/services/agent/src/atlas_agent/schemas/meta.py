from __future__ import annotations

from typing import Literal
from uuid import UUID

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
    #: 模型侧实际出现的工具名，不是 name —— bash 的模型侧名是 execute，filesystem 展开成 5 个。
    model_tool_names: list[str] = []
    #: MCP 工具：来源 server（编辑器按它分组）。内置工具为 None
    server: str | None = None
    #: MCP 工具定义的指纹（保存 agent 时记录，运行时比对 —— 技能 / MCP 设计 §9）
    digest: str | None = None
    #: MCP：ok | invalid | pending_review | rejected。内置工具为 None
    review_status: str | None = None


class McpSubjectOut(BaseModel):
    """编辑器里 MCP 工具按谁的权限列出（MCP 方案设计 §9.2）。只在经 MCP 网关时给出。"""

    #: user = 以人为权限（当前用户）；agent = 以 Agent 为权限（它的数字员工）
    kind: Literal["user", "agent"]
    #: 权限主体（用户中心 uid）。以 Agent 为权限、还没有数字员工时为空
    uid: UUID | None = None
    #: 主体的 API Key：active | disabled | none（没 Key 也可以先选，运行时跳过并提示）
    key_status: str = "none"
    #: 已启用但主体没有权限的服务个数（不列出它们，只显示个数）
    hidden_servers: int = 0
    notes: list[str] = []


class ToolListResponse(BaseModel):
    data: list[ToolInfo]
    mcp_subject: McpSubjectOut | None = None
