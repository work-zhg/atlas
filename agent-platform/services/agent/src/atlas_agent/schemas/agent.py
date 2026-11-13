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
from atlas_engine.contracts.spec import (
    MAX_KNOWLEDGE_SETS,
    AgentSpec,
    ApprovalSpec,
    CliModelSource,
    CliPlacement,
    CliSpec,
    CompactionSpec,
    KnowledgeSpec,
    LimitSpec,
    McpDriftPolicy,
    ModelSpec,
    PermissionMode,
    SandboxProfile,
    SandboxSpec,
    SkillRefSpec,
    SubAgentSpec,
)
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

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
    kind: Literal["native", "cli"] = "native"
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
    #: 运行位置：会话 Pod，或使用者自己的电脑（本机 CLI 方案 §2.1）。建智能体时定下，之后不能改
    placement: CliPlacement = "pod"
    #: 本机 CLI 的默认项目地址（如 ~/atlas-projects）
    default_project_dir: str = Field(default="", max_length=512)
    #: 本机 CLI 的模型来源：平台模型，或电脑上 CLI 自己的登录
    model_source: CliModelSource = "platform"

    def to_engine(self) -> CliSpec:
        return CliSpec(
            cli_type=self.cli_type,
            adapter=self.adapter,
            image=self.image,
            permission_mode=self.permission_mode,
            placement=self.placement,
            default_project_dir=self.default_project_dir.strip(),
            model_source=self.model_source,
        )


class SandboxSpecIn(BaseModel):
    """native 的 Shell 沙箱（见 domain/spec.py::SandboxSpec）。快照纪律：字段都有默认值。"""

    #: 空 = 平台默认镜像
    image: str = Field(default="", max_length=256)
    profile: SandboxProfile = "strict"

    def to_engine(self) -> SandboxSpec:
        return SandboxSpec(image=self.image.strip(), profile=self.profile)


class KnowledgeSpecIn(BaseModel):
    """智能体能查的知识：选定的几个知识集（set_ids），或使用者能读的全部知识集。

    ★ 知识集归知识库服务（名称、介绍、里面的库由维护人定），这里只记选了哪些。
    ★ 快照纪律：旧快照有两种 ——
      · 知识集之前：{dataset_ids, mode, scope, top_k, score_threshold, rerank}
      · 知识集在智能体里定义时：{sets: [{name, description, dataset_ids, scope, …}]}
      旧字段只读入、不写出（exclude）。知识库迁移 k0008 给每个库建了一个**同 id** 的知识集，
      所以旧的库 id 原样就是知识集 id；旧配置里有「使用者能读的全部」的，读成全部知识集。
    """

    scope: Literal["selected", "all_readable"] = "selected"
    set_ids: list[str] = Field(default_factory=list, max_length=MAX_KNOWLEDGE_SETS)
    # ── 旧字段：不进接口文档（SkipJsonSchema），也不写出（exclude）
    sets: SkipJsonSchema[list[dict[str, Any]]] = Field(default_factory=list, exclude=True)
    dataset_ids: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)
    mode: SkipJsonSchema[str] = Field(default="tool", exclude=True)
    top_k: SkipJsonSchema[int] = Field(default=4, exclude=True)
    score_threshold: SkipJsonSchema[float | None] = Field(default=None, exclude=True)
    rerank: SkipJsonSchema[bool] = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _from_legacy(self) -> KnowledgeSpecIn:
        if self.scope == "all_readable":
            self.set_ids = []
            return self
        if self.set_ids:
            self.set_ids = list(dict.fromkeys(self.set_ids))
            return self
        if any(s.get("scope") == "all_readable" for s in self.sets):
            self.scope = "all_readable"
            return self
        legacy = [*self.dataset_ids, *(d for s in self.sets for d in s.get("dataset_ids") or [])]
        # 旧配置最多 10 个知识集 × 20 个库：超出新上限的截掉（编辑器会提示重新选）
        self.set_ids = list(dict.fromkeys(str(d) for d in legacy))[:MAX_KNOWLEDGE_SETS]
        return self

    def selected_set_ids(self) -> list[str]:
        """选定的知识集（「使用者能读的全部」为空）：保存时校验、身份页签用。"""
        return self.set_ids if self.scope == "selected" else []

    def to_engine(self) -> KnowledgeSpec:
        return KnowledgeSpec(scope=self.scope, set_ids=tuple(self.selected_set_ids()))


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
    kind: Literal["native", "cli"] = "native"
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
    #: native 的 Shell 沙箱（默认平台镜像、strict 档 —— 历史快照没有这个字段）
    sandbox: SandboxSpecIn = Field(default_factory=SandboxSpecIn)

    @property
    def uses_device(self) -> bool:
        """自己或某个子智能体是本机 CLI（跑在使用者的电脑上）。"""
        clis = [self.cli, *(s.cli for s in self.subagents)]
        return any(c is not None and c.placement == "device" for c in clis)

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
            sandbox=self.sandbox.to_engine(),
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
    """选定的一个知识集。"""

    id: str
    name: str | None = None
    #: 按这个 Agent 的权限类型判：以 Agent 为权限 = 数字员工能不能用（其中至少有一个库能读）；
    #: 以人为权限 = 不适用（None）
    readable: bool | None = None


class AgentIdentityOut(BaseModel):
    """「身份」页签（数字员工设计 §6.1）：数字员工、权限类型、有效权限摘要。"""

    auth_subject: AuthSubject
    digital_employee: DigitalEmployeeOut | None = None
    #: Agent 选了哪些知识集、按权限类型能不能用（以人为权限时因人而异，readable 为空）
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


class DefaultAgentIn(BaseModel):
    #: None = 取消默认
    agent_id: UUID | None = None


class DefaultAgentOut(BaseModel):
    agent_id: UUID | None = None


class AgentVersionOut(BaseModel):
    id: UUID
    version: int
    spec: dict[str, Any]
    created_at: datetime
    #: 发布时写的「这次改了什么」
    note: str = ""


class AgentDraftIn(BaseModel):
    """编辑器自动保存的草稿：只校验结构，引用与引擎规则留到发布时校验。"""

    spec: AgentSpecIn


class AgentPublishIn(BaseModel):
    #: 这次改了什么（可选）
    note: str = Field(default="", max_length=500)


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
    #: CLI 智能体跑在哪：pod | device（本机 CLI）；native 为空
    placement: str | None = None
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
    #: 当前主体对它的有效级别（READ / WRITE / OWNER）：前端据此决定能不能编辑、共享
    my_level: str | None = None
    #: 是不是我创建的（列表的「我创建的 / 共享给我的」）
    mine: bool = False
    #: 创建者姓名
    owner_name: str | None = None
    #: 我最近一次用它的时间（我的会话里最近的更新）
    last_used_at: datetime | None = None
    #: 有没有未发布的修改
    has_draft: bool = False


class AgentDetailOut(AgentOut):
    spec: dict[str, Any]
    #: 未发布的修改（编辑器打开时用它；None = 和当前版本一样）
    draft_spec: dict[str, Any] | None = None
    draft_updated_at: datetime | None = None


class AgentListOut(BaseModel):
    data: list[AgentOut]
    next_cursor: str | None = None


class AgentVersionListOut(BaseModel):
    data: list[AgentVersionOut]
