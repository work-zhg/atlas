"""智能体 API schema（文档 §11.1）。

这里是 server 与 engine 的接缝：请求体 → engine 的 AgentSpec dataclass。
**保存时就调用 spec.validate()**，于是"opus-5 不收 temperature"这类错误
在编辑器点保存的瞬间就被拒，而不是等到某次 run 才 400（§3 D3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from atlas_server.domain.spec import (
    AgentSpec,
    CliSpec,
    SkillRefSpec,
    CompactionSpec,
    LimitSpec,
    ModelSpec,
    SubAgentSpec,
)
from atlas_server.domain.tool_registry import BASH_TOOL
from pydantic import BaseModel, Field, field_validator, model_validator

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Thinking = Literal["auto", "adaptive", "off"]
AgentStatus = Literal["draft", "enabled", "archived"]
SessionMode = Literal["persistent", "ephemeral"]

SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"


class ModelSpecIn(BaseModel):
    model: str
    provider: Literal["anthropic", "openai"] = "anthropic"
    effort: Effort | None = None
    thinking: Thinking = "auto"
    max_output_tokens: int = Field(default=16_384, gt=0)
    prompt_cache: bool = True
    temperature: float | None = Field(default=None, ge=0, le=1)

    def to_engine(self) -> ModelSpec:
        return ModelSpec(**self.model_dump())


class SkillRefIn(BaseModel):
    """agent 引用一个技能。

    ★ version=None 只在**请求体**里合法，表示"用当前版本"。服务层在落库
      前解析成具体版本号 —— 于是存进 agent_version.spec 的永远是确定版本，
      历史 run 可精确复现，技能升级不会悄悄改变已有配置的含义。

    ★ 快照纪律（tests/test_spec_snapshots.py）：必填集合只有 slug，
      version 必须有默认值。
    """

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    version: int | None = None

    def to_engine(self) -> SkillRefSpec:
        # 落库前已被服务层解析，此处 version 必非空
        return SkillRefSpec(slug=self.slug, version=self.version or 0)


class CompactionSpecIn(BaseModel):
    enabled: bool = True
    trigger_ratio: float = Field(default=0.75, gt=0, lt=1)
    target_ratio: float = Field(default=0.40, gt=0, lt=1)
    keep_recent_turns: int = Field(default=3, ge=1)
    summarizer_model: str = "claude-haiku-4-5"

    def to_engine(self) -> CompactionSpec:
        return CompactionSpec(**self.model_dump())


class SubAgentSpecIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=1024)
    system_prompt: str = Field(default="", max_length=100_000)
    model: ModelSpecIn
    tool_names: list[str] = Field(default_factory=list)
    #: 子智能体自己的技能。拷进**子会话自己的** skills 前缀 —— workspace 是
    #: 共享挂载的，技能不是（detail/subagent.html §03）。
    skills: list[SkillRefIn] = Field(default_factory=list, max_length=32)
    #: persistent = 持有一个子会话，第二次委派恢复上次的上下文（默认）。
    #: ephemeral  = 每次新建、跑完归档，因此可以并行。
    session_mode: SessionMode = "persistent"
    #: 子会话的压缩配置。None = 继承主 agent 的。
    compaction: CompactionSpecIn | None = None
    #: 子智能体自己的执行形态 —— native 主 agent 可以委派给 acp 子智能体。
    kind: Literal["native", "acp"] = "native"
    cli: CliSpecIn | None = None
    #: 这份配置是从哪个智能体**物化**来的，仅供编辑器展示来源。
    #:
    #: ★ 存的是拷贝而不是活引用：spec 是版本快照，run 绑 agent_version_id
    #:   才能回答"当时用的哪份配置"（§5.4）。若这里存 id 在执行时解引用，
    #:   来源智能体一改，所有历史 run 的含义就跟着变了。
    #:   因此它不进 to_engine —— engine 永远只看到物化后的副本。
    source_agent_id: UUID | None = None

    def to_engine(self) -> SubAgentSpec:
        return SubAgentSpec(
            name=self.name,
            description=self.description,
            system_prompt=self.system_prompt,
            model=self.model.to_engine(),
            tool_names=tuple(self.tool_names),
            skills=tuple(s.to_engine() for s in self.skills),
            session_mode=self.session_mode,
            compaction=self.compaction.to_engine() if self.compaction else None,
            kind=self.kind,
            cli=self.cli.to_engine() if self.cli else None,
        )



class LimitSpecIn(BaseModel):
    max_steps: int = Field(default=40, gt=0)
    timeout_s: int = Field(default=300, gt=0)
    max_total_tokens: int = Field(default=500_000, gt=0)
    max_subagent_depth: int = Field(default=2, ge=0)
    tool_concurrency: int = Field(default=4, gt=0)
    require_approval_for: list[str] = Field(default_factory=list)

    def to_engine(self) -> LimitSpec:
        return LimitSpec(
            max_steps=self.max_steps,
            timeout_s=self.timeout_s,
            max_total_tokens=self.max_total_tokens,
            max_subagent_depth=self.max_subagent_depth,
            tool_concurrency=self.tool_concurrency,
            require_approval_for=frozenset(self.require_approval_for),
        )



class CliSpecIn(BaseModel):
    """acp agent 的 CLI 形态。快照纪律：除 cli_type 外都要有默认值。"""

    cli_type: str = Field(min_length=1, max_length=64)
    adapter: str = Field(default="", max_length=512)
    image: str = Field(default="", max_length=256)

    def to_engine(self) -> CliSpec:
        return CliSpec(cli_type=self.cli_type, adapter=self.adapter, image=self.image)


class AgentSpecIn(BaseModel):
    """agent_version.spec 的请求形态。slug / name 由 agent 行提供，不在这里重复。

    ★ 快照纪律：新字段必须有默认值 —— agent_version.spec 是历史数据，
      加必填会让所有历史快照反序列化失败。守卫见
      tests/test_spec_snapshots.py。
    """

    system_prompt: str = Field(default="", max_length=100_000)
    model: ModelSpecIn
    tool_names: list[str] = Field(default_factory=list)
    subagents: list[SubAgentSpecIn] = Field(default_factory=list, max_length=20)
    #: 默认空列表 —— 历史快照没有这个字段，反序列化照常
    skills: list[SkillRefIn] = Field(default_factory=list, max_length=32)
    limits: LimitSpecIn = Field(default_factory=LimitSpecIn)
    compaction: CompactionSpecIn = Field(default_factory=CompactionSpecIn)
    #: 默认 native —— 历史快照没有这两个字段，反序列化照常
    kind: Literal["native", "acp"] = "native"
    cli: CliSpecIn | None = None

    @model_validator(mode="after")
    def _bash_requires_approval(self) -> AgentSpecIn:
        """勾了 bash 强制 execute 进审批列表（docs/sandbox.md D2，S1 收紧版）。

        写在 schema 层而不是编辑器默认值：提示词不是访问控制，前端默认值
        也不是 —— 绕过编辑器直接调 API 的请求同样要被拦住。
        模型侧的工具名是 execute（D4），审批中间件按它匹配。
        """
        if BASH_TOOL in self.tool_names and "execute" not in self.limits.require_approval_for:
            self.limits.require_approval_for = [*self.limits.require_approval_for, "execute"]
        return self

    def to_engine(self, *, slug: str, name: str) -> AgentSpec:
        return AgentSpec(
            slug=slug,
            name=name,
            system_prompt=self.system_prompt,
            model=self.model.to_engine(),
            tool_names=tuple(self.tool_names),
            subagents=tuple(s.to_engine() for s in self.subagents),
            skills=tuple(s.to_engine() for s in self.skills),
            limits=self.limits.to_engine(),
            compaction=self.compaction.to_engine(),
            kind=self.kind,
            cli=self.cli.to_engine() if self.cli else None,
        )


class AgentCreate(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    avatar_key: str = Field(default="general", max_length=32)
    spec: AgentSpecIn


class AgentUpdate(BaseModel):
    """PATCH 语义：保存即产生新版本，不做原地更新（文档 §11.1 / §5.4）。

    slug 创建后不可改 —— 它是 API 引用键。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2048)
    avatar_key: str | None = Field(default=None, max_length=32)
    spec: AgentSpecIn | None = None

    @field_validator("*")
    @classmethod
    def _at_least_something(cls, v: Any) -> Any:
        return v


class AgentStatusUpdate(BaseModel):
    status: AgentStatus


class AgentVersionOut(BaseModel):
    id: UUID
    version: int
    spec: dict[str, Any]
    created_at: datetime


class AgentOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    avatar_key: str
    status: AgentStatus
    is_builtin: bool
    version: int
    # 列表页要展示的摘要，避免前端为每张卡片再拉一次 spec
    model: str
    tool_count: int
    subagent_count: int
    created_at: datetime
    updated_at: datetime


class AgentDetailOut(AgentOut):
    spec: dict[str, Any]


class AgentListOut(BaseModel):
    data: list[AgentOut]
    next_cursor: str | None = None


class AgentVersionListOut(BaseModel):
    data: list[AgentVersionOut]
