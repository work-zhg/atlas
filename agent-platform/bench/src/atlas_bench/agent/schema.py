"""统一任务模型与实验配置（doc/agent-eval-design.html §7 / §10）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "AgentExperimentConfig",
    "Budget",
    "Expectation",
    "Task",
    "TrialResult",
    "VariantConfig",
    "Verifier",
]


class Budget(BaseModel):
    """任务预算：评测协议，由运行器执行（超出 → 取消 run，记 budget_exceeded）。不是平台限制。"""

    wall_s: float = 900.0
    max_tokens: int = 2_000_000


class Verifier(BaseModel):
    """校验：Agent 结束后，把最终工作区拷进隔离容器（无网络），跑任务自带的校验脚本。

    ★ 校验脚本放在任务的 verifier/ 目录，挂到容器的 /verifier —— Agent 从头到尾看不到它
      （否则就成了照着答案做题）。退出码 0 = 通过；脚本可以把「部分得分」写到 stdout 的
      最后一行 `SCORE=<0~1>`。
    """

    command: str = "python /verifier/verify.py"
    image: str = "python:3.12-slim"
    timeout_s: float = 120.0


#: 审批策略：approve_all / deny_all / deny_first（本 trial 第一次拒绝，其余批准）/
#: delay:<秒>（等这么久再批准 —— 测长时间待审批）
_POLICY = re.compile(r"^(approve_all|deny_all|deny_first|delay:\d+)$")


def _check_policy(v: str | None) -> str | None:
    if v is not None and not _POLICY.match(v):
        raise ValueError(f"审批策略 {v!r} 不认识：approve_all | deny_all | deny_first | delay:<秒>")
    return v


class Expectation(BaseModel):
    """过程断言（doc/agent-platform-eval-plan.html W1）：从轨迹判定工程机制有没有按预期工作。

    ★ 只用来断言**工程机制**（压缩发生了、子智能体并行了、没有重复执行），不断言 Agent
      「必须用某种办法做事」—— 后者以结果为准，写成断言会惩罚其他正确做法。

        event   轨迹事件类型（如 context.compacted），统计条数；where 按 data 字段相等过滤
        metric  过程指标名（trajectory.metrics_from_trace 的键，如 max_parallel_subagents）
        count   比较式：>=n / <=n / ==n / >n / <n
    """

    desc: str = ""
    #: True = 不变量（违反即平台缺陷，进硬门槛，如「事件不丢不重」「不重复执行」）；
    #: False = 场景前提（「审批确实被拒过」）：不满足只说明模型这次没走到这条路径，
    #: 场景没生效 —— 单列为「场景未生效」，不进硬门槛（否则 L1 会随模型的随机性红绿跳）
    gate: bool = True
    event: str | None = None
    metric: str | None = None
    where: dict[str, Any] = Field(default_factory=dict)
    count: str = ">=1"

    @field_validator("count")
    @classmethod
    def _count(cls, v: str) -> str:
        if not re.match(r"^(>=|<=|==|>|<)\d+$", v.replace(" ", "")):
            raise ValueError(f"count 写成 >=n / <=n / ==n / >n / <n，而不是 {v!r}")
        return v.replace(" ", "")


class Task(BaseModel):
    id: str  # <套件>/<任务>
    suite: str
    category: Literal["cli", "code", "data", "tool", "dialog", "security", "eng"] = "cli"
    tags: list[str] = Field(default_factory=list)
    instruction: str
    #: 初始工作区：相对路径 → 内容（来自任务的 files/ 目录）
    files: dict[str, str] = Field(default_factory=dict)
    verifier: Verifier = Field(default_factory=Verifier)
    budget: Budget = Field(default_factory=Budget)
    #: 任务目录（校验脚本在 <dir>/verifier/）
    dir: str = ""
    #: 过程断言（全部满足才算 expect_ok；不影响 outcome，报告里单列）
    expect: list[Expectation] = Field(default_factory=list)
    #: 覆盖实验级的审批策略（如审批被拒场景只在这一题用 deny_first）
    approval_policy: str | None = None
    #: 叠加到被测配置 spec 上的补丁（深合并：字典递归合并、列表整体替换）。
    #: 场景需要特殊配置时用（给写文件加审批、配一个 ephemeral 子智能体）
    agent_patch: dict[str, Any] = Field(default_factory=dict)
    #: 前置会话（W4）：主任务之前，先在**另一个**会话里按顺序发这些消息（各等到终态）。
    #: 用于跨会话场景（会话 A 交代偏好 → 会话 B 检验）
    setup: list[str] = Field(default_factory=list)
    #: 会话的用户记忆开关（前置会话与主会话都用它）。评测默认关，避免互相污染
    thread_memory: Literal["on", "off"] = "off"
    #: 前置会话结束后最多等多久让记忆抽取出来（异步入队抽取）；0 = 不等
    wait_memory_s: float = 0.0
    #: 同一任务的多次 trial 不并发（共享状态的场景，如用户记忆）
    serial: bool = False
    #: SSE 断线重连检测（E18）：收到这么多条事件后主动断开，稍后带游标重连
    sse_disconnect_after: int | None = None

    @field_validator("approval_policy")
    @classmethod
    def _policy(cls, v: str | None) -> str | None:
        return _check_policy(v)


class VariantConfig(BaseModel):
    """一个被测方配置。adapter=atlas 时 agent 是 Atlas 的 AgentSpec（JSON）。"""

    adapter: Literal["atlas"] = "atlas"
    agent: dict[str, Any]
    description: str = ""


class AgentExperimentConfig(BaseModel):
    name: str
    kind: Literal["agent"] = "agent"
    suites: list[str]
    #: 只跑带这些标签之一的任务（空 = 全部）
    tags_any: list[str] = Field(default_factory=list)
    limit: int | None = None
    trials: int = 3
    concurrency: int = 3
    #: approve_all：能力评测，不让审批成为变量（次数与等待仍计入指标）
    approval_policy: str = "approve_all"
    variants: dict[str, VariantConfig]
    #: 报告里要做的配对比较：[基线, 对照]
    comparisons: list[tuple[str, str]] = Field(default_factory=list)
    #: 评测结束后删除评测建的会话（智能体保留，下次复用）
    delete_threads: bool = True

    @field_validator("approval_policy")
    @classmethod
    def _policy(cls, v: str | None) -> str | None:
        return _check_policy(v)


class TrialResult(BaseModel):
    task_id: str
    variant: str
    trial: int
    #: success | failure | budget_exceeded | agent_error | platform_error | infra_error
    #:   agent_error     run 失败，原因在 Agent / harness（超步数、上下文溢出、模型拒答）
    #:   platform_error  run 失败或沙箱不可用，原因在基础设施（网关、Pod、内部错误）；
    #:                   按协议重跑过一次仍失败才记。计入成功率分母，报告首页单列
    #:   infra_error     评测侧的问题（建会话失败、校验容器起不来），不计入分母，续跑时重来
    outcome: str
    passed: bool = False
    score: float = 0.0
    verifier_output: str = ""
    error: str | None = None
    #: 过程指标（trajectory.metrics）
    metrics: dict[str, Any] = Field(default_factory=dict)
    final_message: str = ""
    wall_s: float = 0.0
    refs: dict[str, Any] = Field(default_factory=dict)
    #: 过程断言：[{desc, ok, actual, count}]；expect_ok=None 表示这题没有断言
    expectations: list[dict[str, Any]] = Field(default_factory=list)
    expect_ok: bool | None = None
    #: 因平台错误重跑过（协议允许一次）
    retried: bool = False
