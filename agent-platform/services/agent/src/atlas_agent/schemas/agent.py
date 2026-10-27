"""智能体 API schema（文档 §11.1）。

这里是 server 与 engine 的接缝：请求体 → engine 的 AgentSpec dataclass。
**保存时就调用 spec.validate()**，于是"opus-5 不收 temperature"这类错误
在编辑器点保存的瞬间就被拒，而不是等到某次 run 才 400（§3 D3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from atlas_engine.contracts import APPROVAL_MODES, ApprovalMode, InvalidSpec
from pydantic import BaseModel, Field, field_validator

from atlas_agent.domain.spec import (
    AgentSpec,
    ApprovalSpec,
    CliSpec,
    CompactionSpec,
    KnowledgeSpec,
    LimitSpec,
    McpDriftPolicy,
    ModelSpec,
    PermissionMode,
    SkillRefSpec,
    SubAgentSpec,
)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Thinking = Literal["auto", "adaptive", "off"]
AgentStatus = Literal["draft", "enabled", "archived"]
#: 权限类型（数字员工设计 §7.1）：user = 以人为权限，agent = 以 Agent 为权限
AuthSubject = Literal["user", "agent"]
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
        # ★ 落库前服务层必须已把 version 钉死（AgentService._pin_skills）。
        #   此前这里写的是 `version or 0`：没钉死的引用悄悄变成 v0，建会话时才因
        #   找不到 _skills/<slug>/0/ 失败 —— 报错离病根很远。
        if self.version is None:
            raise InvalidSpec(f"技能 {self.slug} 没有钉死版本号", skill=self.slug)
        return SkillRefSpec(slug=self.slug, version=self.version)


class CompactionSpecIn(BaseModel):
    enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0, lt=1)
    target_ratio: float = Field(default=0.40, gt=0, lt=1)
    keep_recent_turns: int = Field(default=3, ge=1)
    #: 空 = 用智能体自己的模型
    summarizer_model: str = ""
    clear_ratio: float = Field(default=0.6, gt=0, lt=1)
    warn_ratio: float = Field(default=0.9, gt=0, lt=1)
    keep_recent_tool_results: int = Field(default=5, ge=0, le=100)
    restore_files: int = Field(default=5, ge=0, le=20)
    attachment_budget_ratio: float = Field(default=0.1, gt=0, le=0.5)

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
    #: 见 AgentSpecIn.mcp_tool_digests
    mcp_tool_digests: dict[str, str] = Field(default_factory=dict)
    mcp_drift_policy: McpDriftPolicy = "warn"
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
            mcp_tool_digests=dict(self.mcp_tool_digests),
            mcp_drift_policy=self.mcp_drift_policy,
        )


class LimitSpecIn(BaseModel):
    max_steps: int = Field(default=40, gt=0)
    timeout_s: int = Field(default=300, gt=0)
    max_total_tokens: int = Field(default=500_000, gt=0)
    tool_concurrency: int = Field(default=4, gt=0)
    # ★ require_approval_for 已删除（问不问人改由审批模式决定，见 ApprovalSpecIn）。
    #   历史快照里带着它也能照常读：pydantic 默认忽略多余字段，再保存时自然丢掉

    def to_engine(self) -> LimitSpec:
        return LimitSpec(
            max_steps=self.max_steps,
            timeout_s=self.timeout_s,
            max_total_tokens=self.max_total_tokens,
            tool_concurrency=self.tool_concurrency,
        )


class ApprovalSpecIn(BaseModel):
    """审批模式的默认值与允许范围（doc/native-approval-mode-design.html）。

    快照纪律：字段都有默认值 —— 历史快照没有这一段，按「默认自动、允许全部」处理（D8）。
    """

    default_mode: ApprovalMode = "auto"
    allowed_modes: list[ApprovalMode] = Field(default_factory=lambda: list(APPROVAL_MODES))

    def to_engine(self) -> ApprovalSpec:
        # 按严格程度排好、去重：前端与判定都依赖这个顺序
        allowed = tuple(m for m in APPROVAL_MODES if m in self.allowed_modes)
        return ApprovalSpec(default_mode=self.default_mode, allowed_modes=allowed)


class CliSpecIn(BaseModel):
    """acp agent 的 CLI 形态。快照纪律：除 cli_type 外都要有默认值。"""

    cli_type: str = Field(min_length=1, max_length=64)
    adapter: str = Field(default="", max_length=512)
    image: str = Field(default="", max_length=256)
    #: CLI 的权限模式。默认 auto；不提供 bypass（见 domain/spec.py::PermissionMode）
    permission_mode: PermissionMode = "auto"

    def to_engine(self) -> CliSpec:
        return CliSpec(
            cli_type=self.cli_type,
            adapter=self.adapter,
            image=self.image,
            permission_mode=self.permission_mode,
        )


class KnowledgeSpecIn(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["auto", "tool"] = "auto"
    top_k: int = Field(default=4, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    rerank: bool = False

    def to_engine(self) -> KnowledgeSpec:
        return KnowledgeSpec(
            dataset_ids=tuple(dict.fromkeys(self.dataset_ids)),
            mode=self.mode,
            top_k=self.top_k,
            score_threshold=self.score_threshold,
            rerank=self.rerank,
        )


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
    #: 保存时记录的 MCP 工具定义指纹 {mcp:server:tool → digest}。
    #: ★ 服务端填写（AgentService），请求里传来的值会被覆盖 —— 指纹的意义是
    #:   「保存那一刻实际看到的定义」，不能由客户端声称。
    mcp_tool_digests: dict[str, str] = Field(default_factory=dict)
    #: 定义漂移时：warn = 提示后照常用；block = 该工具不装（技能 / MCP 设计 §9）
    mcp_drift_policy: McpDriftPolicy = "warn"
    #: 绑定的知识库（默认不绑定 —— 历史快照没有这个字段）
    knowledge: KnowledgeSpecIn = Field(default_factory=KnowledgeSpecIn)
    #: 审批模式（默认自动、允许全部 —— 历史快照没有这个字段）。
    #: ★ 原先「勾了 bash 强制 execute 进必审列表」的校验已删除：命令执行由审批模式管 ——
    #:   手动请示、自动交判断器、规划拦下，都在中间件里强制执行，不靠提示词（设计 D7）
    approval: ApprovalSpecIn = Field(default_factory=ApprovalSpecIn)

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
            mcp_tool_digests=dict(self.mcp_tool_digests),
            mcp_drift_policy=self.mcp_drift_policy,
            knowledge=self.knowledge.to_engine(),
            approval=self.approval.to_engine(),
        )


class AgentCreate(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    avatar_key: str = Field(default="general", max_length=32)
    spec: AgentSpecIn
    #: 创建时选；界面必选（默认选中 user）。接口缺省 user，已有调用方（TeamFlow、评测）不受影响
    auth_subject: AuthSubject = "user"


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


class AuthSubjectUpdate(BaseModel):
    auth_subject: AuthSubject


class DigitalEmployeeOut(BaseModel):
    id: UUID
    account: str | None = None
    name: str | None = None
    status: str | None = None
    owner_id: UUID | None = None


class KnowledgeAccessOut(BaseModel):
    id: str
    name: str | None = None
    #: 按这个 Agent 的权限类型判：以 Agent 为权限 = 数字员工能不能读；以人为权限 = 不适用（None）
    readable: bool | None = None


class AgentIdentityOut(BaseModel):
    """「身份」页签（数字员工设计 §6.1）：数字员工、权限类型、有效权限摘要。"""

    auth_subject: AuthSubject
    digital_employee: DigitalEmployeeOut | None = None
    #: Agent 配了哪些知识库、按权限类型能不能读（以人为权限时因人而异，readable 为空）
    knowledge: list[KnowledgeAccessOut] = []
    notices: list[str] = []


class GrantSubject(BaseModel):
    type: Literal["user", "dept"]
    id: str


class AgentGrantCreate(BaseModel):
    level: Literal["READ", "WRITE", "OWNER"]
    subjects: list[GrantSubject] = Field(min_length=1)
    include_sub: bool = True


class AgentGrantPatch(BaseModel):
    level: Literal["READ", "WRITE", "OWNER"] | None = None
    include_sub: bool | None = None


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
    version: int
    # 列表页要展示的摘要，避免前端为每张卡片再拉一次 spec
    model: str
    tool_count: int
    subagent_count: int
    #: native | acp
    kind: str = "native"
    #: 内置工具数（不含 mcp:*）
    builtin_tool_count: int = 0
    skill_count: int = 0
    #: 引用的 MCP server（去重，按名排序）
    mcp_servers: list[str] = []
    #: 委派关系：[{name, kind, permission_mode}] —— 卡片上显示「→ claude-code · Auto」
    subagents: list[dict[str, Any]] = []
    #: 权限类型（数字员工设计 §7.1）
    auth_subject: AuthSubject = "user"
    digital_employee_uid: UUID | None = None
    created_at: datetime
    updated_at: datetime


class AgentDetailOut(AgentOut):
    spec: dict[str, Any]
    #: 当前主体对它的有效级别（READ / WRITE / OWNER）：前端据此决定能不能编辑、共享
    my_level: str | None = None


class AgentListOut(BaseModel):
    data: list[AgentOut]
    next_cursor: str | None = None


class AgentVersionListOut(BaseModel):
    data: list[AgentVersionOut]
