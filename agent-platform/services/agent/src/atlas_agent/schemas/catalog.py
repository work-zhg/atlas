"""技能 / MCP 只读视图的响应体（技能 / MCP 设计 §13.3）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from atlas_config.schemas import SkillVersionBrief
from pydantic import BaseModel, Field


class SkillReference(BaseModel):
    agent_id: UUID
    agent_slug: str
    agent_name: str
    agent_version: int
    #: None = 主智能体自己引用；否则是子智能体名
    subagent: str | None = None
    skill_version: int
    #: 该 agent 未归档的会话数（下架时「受影响的会话」）
    active_threads: int = 0


class SkillListItem(BaseModel):
    slug: str
    source: str
    latest: int | None = None
    description: str | None = None
    has_scripts: bool | None = None
    versions: list[SkillVersionBrief] = Field(default_factory=list)
    references: int = 0
    loads_7d: int = 0


class SkillListOut(BaseModel):
    #: False = 没接配置服务：列表为空，技能引用需手写版本号
    directory_configured: bool
    data: list[SkillListItem]


class SkillReferencesOut(BaseModel):
    data: list[SkillReference]


class SkillUsageItem(BaseModel):
    slug: str
    version: int
    agent_id: UUID
    loads: int
    completed_runs: int


class SkillUsageOut(BaseModel):
    days: int
    data: list[SkillUsageItem]


class McpToolOut(BaseModel):
    name: str
    #: spec 标识 mcp:server:tool
    id: str
    model_name: str | None
    description: str
    digest: str
    issues: list[str] = Field(default_factory=list)
    #: ok | invalid | pending_review | rejected
    review_status: str
    #: 与上一版定义相比：None = 没变；added / description / schema
    change: str | None = None
    previous_description: str | None = None


class McpServerSummary(BaseModel):
    name: str
    url: str
    transport: str
    credential_scope: str
    #: 只回显占位符；看起来含明文的值显示为 "••••"
    headers: dict[str, str]
    call_timeout_s: float | None
    enabled: bool
    review_required: bool
    #: ok | changed | stale | error
    status: str
    error: str | None = None
    tools_count: int = 0
    invalid_tools: int = 0
    pending_review: int = 0
    content_hash: str | None = None
    fetched_at: datetime | None = None
    changed_at: datetime | None = None
    references: int = 0


class McpServerListOut(BaseModel):
    registry: str
    data: list[McpServerSummary]


class McpServerDetailOut(McpServerSummary):
    tools: list[McpToolOut] = Field(default_factory=list)
    removed_tools: list[str] = Field(default_factory=list)
    referenced_by: list[dict[str, object]] = Field(default_factory=list)
