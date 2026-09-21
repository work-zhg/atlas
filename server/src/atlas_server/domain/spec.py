"""AgentSpec —— 一次 run 的配置词汇（文档 §4.1）。

★ 住在 server：kernel 被方向守卫禁止认识它（中间件只收标量），
  消费者只剩 server 自己 —— schemas 翻译进来、build.py 装配、runner 读
  limits。「engine 的唯一输入」的说法已随 agent 防腐层一起退役。

frozen=True 是刻意的：一次 run 期间配置不可变，避免"跑到一半配置被改"
的不可复现问题。engine 不认识数据库，server 负责把 agent_version 行翻译成 spec。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Literal

from atlas_engine.contracts import InvalidSpec

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# auto = 按模型能力自动选择（推荐默认）；adaptive / off 是显式要求，不支持就报错
Thinking = Literal["auto", "adaptive", "off"]
Provider = Literal["anthropic", "openai"]

# 实测：haiku-4-5 传 adaptive 会 400
#   "adaptive thinking is not supported on this model"
# adaptive 是 4.6+ 才有的模式，旧模型只有 budget_tokens 式的 extended thinking。
_ADAPTIVE_THINKING_OK: frozenset[str] = frozenset(
    {"claude-opus-5", "claude-sonnet-5", "claude-fable-5"}
)

# fable-5 思考恒开，显式传 disabled 会 400
_THINKING_ALWAYS_ON: frozenset[str] = frozenset({"claude-fable-5"})

# 实测：haiku-4-5 传 output_config.effort 会 400
#   "This model does not support the effort parameter."
_EFFORT_OK: frozenset[str] = frozenset({"claude-opus-5", "claude-sonnet-5", "claude-fable-5"})

# 对 2026-08-19 网关实测的编码（见 docs/backend-design.md §3）：
#   temperature / top_p 在 opus-5 / sonnet-5 / fable-5 上是硬 400
#   （"`temperature` is deprecated for this model."），仅 haiku-4-5 接受。
# 这份集合是 engine 侧的兜底校验；权威来源是 DB 的 model_catalog.supports_temperature。
_TEMPERATURE_OK: frozenset[str] = frozenset({"claude-haiku-4-5"})

# thinking=off 在 opus-5 上仅 effort <= high 合法，xhigh/max 会 400。
_EFFORT_FORBIDS_THINKING_OFF: frozenset[str] = frozenset({"xhigh", "max"})


@dataclass(frozen=True)
class ModelSpec:
    model: str
    provider: Provider = "anthropic"
    # ★ effort 取代 temperature 成为主要采样控制（网关实测：见上方注释）。
    #   None = 自动：能力允许时用 high，否则完全不发送该参数。
    effort: Effort | None = None
    thinking: Thinking = "auto"
    max_output_tokens: int = 16_384
    prompt_cache: bool = True
    # ⚠️ 仅当 model_catalog.supports_temperature 为真时可设（实质只有 haiku-4-5）。
    #    None = 不发送该参数，这也是 opus/sonnet/fable 唯一合法的取值。
    temperature: float | None = None

    def resolve_effort(self) -> Effort | None:
        """None 表示**完全不发送 effort 参数**（haiku-4-5 只能这样）。"""
        if self.effort is None:
            return "high" if self.model in _EFFORT_OK else None
        return self.effort

    def resolve_thinking(self) -> Literal["adaptive", "off", "none"]:
        """把 thinking 请求解析成实际要发的形态。

        "none" 表示**完全不发送 thinking 参数** —— 这是不支持 adaptive 的模型
        （如 haiku-4-5）唯一安全的选择。
        """
        if self.thinking == "auto":
            return "adaptive" if self.model in _ADAPTIVE_THINKING_OK else "none"
        return self.thinking

    def validate(self) -> None:
        if self.temperature is not None and self.model not in _TEMPERATURE_OK:
            raise InvalidSpec(
                f"model {self.model!r} 不接受 temperature（网关返回 400）；"
                "请改用 effort 控制，或把 temperature 置为 None",
                model=self.model,
            )
        # 显式要 adaptive 但模型不支持 —— 报错而不是悄悄降级（§13.2）
        if self.thinking == "adaptive" and self.model not in _ADAPTIVE_THINKING_OK:
            raise InvalidSpec(
                f"model {self.model!r} 不支持 adaptive thinking（网关返回 400）；"
                "用 thinking='auto' 让 engine 按模型能力选择",
                model=self.model,
            )
        if self.thinking == "off" and self.model in _THINKING_ALWAYS_ON:
            raise InvalidSpec(
                f"model {self.model!r} 的思考无法关闭（网关返回 400）",
                model=self.model,
            )
        if self.effort is not None and self.model not in _EFFORT_OK:
            raise InvalidSpec(
                f"model {self.model!r} 不支持 effort 参数（网关返回 400）；"
                "把 effort 置为 None 让 engine 按模型能力决定",
                model=self.model,
            )
        if self.thinking == "off" and self.resolve_effort() in _EFFORT_FORBIDS_THINKING_OFF:
            raise InvalidSpec(
                f"thinking=off 与 effort={self.resolve_effort()!r} 冲突（网关返回 400）；"
                "effort 需 <= high，或开启 thinking",
                model=self.model,
            )
        if self.max_output_tokens <= 0:
            raise InvalidSpec("max_output_tokens 必须为正数")


@dataclass(frozen=True)
class CompactionSpec:
    """上下文自动摘要（文档 §7）。"""

    enabled: bool = True
    trigger_ratio: float = 0.75
    target_ratio: float = 0.40
    keep_recent_turns: int = 3
    summarizer_model: str = "claude-haiku-4-5"

    def validate(self) -> None:
        if not 0 < self.target_ratio < self.trigger_ratio < 1:
            raise InvalidSpec(
                "必须满足 0 < target_ratio < trigger_ratio < 1（一次压够，避免抖动击穿缓存）"
            )
        if self.keep_recent_turns < 1:
            raise InvalidSpec("keep_recent_turns 至少为 1")


@dataclass(frozen=True)
class LimitSpec:
    """运行限制（文档 §4.4）。max_total_tokens 是刹车而非硬墙。"""

    max_steps: int = 40
    timeout_s: int = 300
    max_total_tokens: int = 500_000
    max_subagent_depth: int = 2
    tool_concurrency: int = 4
    require_approval_for: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SkillRefSpec:
    """agent 引用的一个技能，版本已钉死。

    ★ 存 (slug, version) 而不是裸 slug：技能升级不会悄悄改变历史 run 的
      含义。保存 agent 时把"用当前版本"解析成具体版本号写进快照 ——
      跟进新版是编辑器里一次显式动作，产生 agent 新版本。

    ★ 也不存技能全文：SKILL.md 可能很长且带附件，塞进 spec 会让快照
      膨胀，且技能一改所有 agent 都要重新发版。
    """

    slug: str
    version: int


#: agent 的执行形态。
#:
#: native —— 进程内跑 LangGraph 图，上下文以 message 表为事实源
#: acp    —— 经 ACP 驱动 Pod 里的 CLI，上下文在 CLI 进程内
#:
#: ★ 不做成「顾问 / 助理」这类角色名：角色是用法，type 是机制。
#:   role 会诱导人往里塞与执行方式无关的策略（Agent 管理 §02）。
AgentKind = Literal["native", "acp"]


@dataclass(frozen=True)
class CliSpec:
    """acp agent 的 CLI 形态。

    ★ 三个字段一起进版本快照且**不可改**：换 cli_type 会换掉整组探测能力
      （Agent 管理 §07），换镜像会换掉 CLI/adapter/bridge 的版本组合。
      要换就建新 agent —— 原地改等于篡改存量会话的历史行为。
    """

    cli_type: str  #: "claude-code" / "codex" …
    adapter: str = ""  #: adapter 启动命令（进 Pod 模板）
    image: str = ""  #: Pod 镜像（cli + adapter + bridge 三件套的版本组合）


#: 子智能体的会话模式。
#:
#: persistent —— 子智能体**持有一个子会话**：第二次委派恢复上次的上下文。
#:   连续性正是目的（「把 auth 迁完」与「修那三个失败用例」是同一件事的
#:   两个阶段），代价是上下文只增不减、同名委派必须串行。
#: ephemeral  —— 每次委派新建子会话、跑完归档。无状态 ⇒ 可以并行，
#:   适合「并行调研五个选项」这类彼此无关的任务。
SessionMode = Literal["persistent", "ephemeral"]


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str  # 主 agent 判断"何时委派"的依据
    system_prompt: str
    model: ModelSpec
    tool_names: tuple[str, ...] = ()
    #: 子智能体自己的技能，与主 agent 的 skills 同构（版本已钉死）。
    #:
    #: ★ 拷进**子会话自己的** skills 前缀，不是主 agent 的 —— workspace 是
    #:   共享挂载的，技能不是。同一个技能被两边引用时拷两份：冗余可接受，
    #:   换来的是各自改动互不影响。
    skills: tuple[SkillRefSpec, ...] = ()
    #: 见 SessionMode。默认 persistent —— 连续性是本设计的目的。
    session_mode: SessionMode = "persistent"
    #: 子会话的压缩配置。None = 继承主 agent 的（与 model 的处理方式一致）。
    #: 子会话是「一个专项任务的连续记录」，主会话是「用户与顾问的往来」，
    #: 主会话的 trigger_ratio 未必适合它。
    compaction: CompactionSpec | None = None
    #: 子智能体自己的执行形态。
    #:
    #: ★ 默认 native 而不是「继承父的」：只有 native agent 能委派（acp 的
    #:   工具面由 CLI 自带，平台的 task 工具进不去它的图），所以父恒为
    #:   native，「继承」与「默认 native」在语义上等价而后者更直白。
    #: ★ 委派链路本身不认识它：子会话也是 thread，AcpRuntime 对它原样生效
    #:   （subagent §02 的对称性）。这里只是让子会话的 spec 派生带上它。
    kind: AgentKind = "native"
    #: kind="acp" 时必填。
    cli: CliSpec | None = None


@dataclass(frozen=True)
class AgentSpec:
    slug: str
    name: str
    system_prompt: str
    model: ModelSpec
    tool_names: tuple[str, ...] = ()
    subagents: tuple[SubAgentSpec, ...] = ()
    #: 本 agent 关联的技能（版本已钉死）。会话创建时拷进会话的 skills 前缀。
    skills: tuple[SkillRefSpec, ...] = ()
    limits: LimitSpec = field(default_factory=LimitSpec)
    compaction: CompactionSpec = field(default_factory=CompactionSpec)
    #: 执行形态。★ 有默认值是快照纪律：历史 agent_version.spec 里没有这个
    #: 字段，反序列化必须照常（tests/test_spec_snapshots.py 守着）。
    kind: AgentKind = "native"
    #: kind="acp" 时必填，validate() 互校。
    cli: CliSpec | None = None

    def subagent(self, name: str) -> SubAgentSpec | None:
        """按名字取子智能体配置。子会话执行时用它派生出独立的 AgentSpec。"""
        for sub in self.subagents:
            if sub.name == name:
                return sub
        return None

    def validate(self) -> None:
        self.model.validate()
        self.compaction.validate()
        if self.kind == "acp" and self.cli is None:
            raise InvalidSpec("kind='acp' 必须提供 cli（cli_type / adapter / image）")
        if self.kind == "acp" and any(
            not name.startswith("mcp:") for name in self.tool_names
        ):
            # ★ acp 的工具来自 CLI 自带，平台内置工具对它没有意义。勾了是
            #   配置错误 —— 报出来而不是静默忽略（Agent 管理 §05）。
            raise InvalidSpec(
                "acp agent 只能配 mcp:* 工具；文件与命令工具由 CLI 自带",
                tool_names=sorted(self.tool_names),
            )
        for sub in self.subagents:
            sub.model.validate()
        names = [s.name for s in self.subagents]
        if len(names) != len(set(names)):
            raise InvalidSpec("子智能体名称必须唯一", names=names)
        if self.kind == "acp" and self.subagents:
            # ★ acp agent **不能委派**：平台的 task 工具装在 LangGraph 图上，
            #   而 acp 的一轮根本不建图（工具面由 CLI 自带）。静默忽略这份
            #   配置就是「配了却不生效」—— §13.2 不允许。
            raise InvalidSpec(
                "acp agent 不能配置子智能体：task 工具装在图上，而 acp 不建图。"
                "要让 CLI 调别的东西，用 mcp:* ",
                subagents=[s.name for s in self.subagents],
            )
        for sub in self.subagents:
            if sub.kind == "acp" and sub.cli is None:
                raise InvalidSpec(
                    f"子智能体 {sub.name!r} 的 kind='acp' 但缺少 cli 配置",
                    subagent=sub.name,
                )


def spec_for_subagent(parent: AgentSpec, name: str) -> AgentSpec:
    """父 agent 的 spec + 子智能体名 → 子会话自己的 AgentSpec。

    ★ 这是「子会话 = thread」这个选择最实在的回报：子会话的 run 因此走
      **与普通 run 完全同一条**执行链路 —— 历史从 message 表重建、压缩、
      步数刹车、审批、取消、孤儿回收全部原样复用，一行新代码都不用写。

    两处刻意的收紧：

      subagents=()             深度结构性封顶在 1。会话级模型下嵌套会让
                               「哪个会话恢复哪个」变成一棵要遍历的树。
      max_subagent_depth=0     同一件事的第二道保险 —— 即使将来有人给
                               SubAgentSpec 加了嵌套字段，这里也不装 task。

    compaction 缺省继承父的（与 model 的处理方式一致）；limits 整体继承，
    于是「run 内该子智能体的总预算」与主图语义保持一致。
    """
    sub = parent.subagent(name)
    if sub is None:
        raise InvalidSpec(
            f"智能体 {parent.slug!r} 没有名为 {name!r} 的子智能体",
            agent=parent.slug,
            subagent=name,
        )
    return AgentSpec(
        # slug 带上父的：日志与事件里一眼看出这是谁的哪个子智能体
        slug=f"{parent.slug}/{sub.name}",
        name=sub.name,
        system_prompt=sub.system_prompt,
        model=sub.model,
        tool_names=sub.tool_names,
        subagents=(),
        skills=sub.skills,
        limits=dataclasses.replace(parent.limits, max_subagent_depth=0),
        compaction=sub.compaction or parent.compaction,
        # ★ 用**子智能体自己的**执行形态：native 主 agent 可以委派给
        #   acp 子智能体（「让 Claude Code 去改这三个文件」），反过来不行。
        kind=sub.kind,
        cli=sub.cli,
    )
