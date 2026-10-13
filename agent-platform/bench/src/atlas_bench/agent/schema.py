"""统一任务模型与实验配置（doc/agent-eval-design.html §7 / §10）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "AgentExperimentConfig",
    "Budget",
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
    approval_policy: Literal["approve_all", "deny_all"] = "approve_all"
    variants: dict[str, VariantConfig]
    #: 报告里要做的配对比较：[基线, 对照]
    comparisons: list[tuple[str, str]] = Field(default_factory=list)
    #: 评测结束后删除评测建的会话（智能体保留，下次复用）
    delete_threads: bool = True


class TrialResult(BaseModel):
    task_id: str
    variant: str
    trial: int
    #: success | failure | budget_exceeded | agent_error | infra_error
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
