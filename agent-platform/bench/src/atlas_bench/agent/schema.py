"""统一任务模型与实验配置（doc/agent-eval-design.html §14.2 / §16）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "Action",
    "AgentExperimentConfig",
    "Budget",
    "Expectation",
    "RunExpect",
    "SimulatedUser",
    "Task",
    "TrialResult",
    "Trigger",
    "Turn",
    "VariantConfig",
    "Verifier",
]


class Budget(BaseModel):
    """任务预算：评测协议，由运行器执行（超出 → 取消 run，记 budget_exceeded）。不是平台限制。"""

    wall_s: float = 900.0
    max_tokens: int = 2_000_000


class Verifier(BaseModel):
    """校验：Agent 结束后跑任务自带的校验脚本。两种方式：

        container  把最终工作区拷进隔离容器（无网络）校验。校验脚本挂到 /verifier；
                   退出码 0 = 通过，stdout 最后一行 `SCORE=<0~1>` 给部分得分
        sandbox    在 Agent 用过的沙箱里原地校验（Terminal-Bench 这类：要看的是环境状态，
                   不只是文件）。Agent 结束后才把 verifier/ 解到沙箱的 dest，在 cwd 里执行
                   command，再读 reward_file 里的得分（1 = 通过，同 Harbor）。
                   要求评测档沙箱与平台的直接执行接口（POST /v1/threads/{id}/sandbox/exec）

    ★ 校验脚本放在任务的 verifier/ 目录，Agent 从头到尾看不到它（否则就成了照着答案做题）。
    """

    mode: Literal["container", "sandbox"] = "container"
    command: str = "python /verifier/verify.py"
    #: container 模式的校验镜像
    image: str = "python:3.12-slim"
    timeout_s: float = 120.0
    #: sandbox 模式：verifier/ 解到哪、在哪执行、得分写在哪
    dest: str = "/tests"
    cwd: str = "/app"
    reward_file: str = "/logs/verifier/reward.txt"


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

    def key(self) -> str:
        """断言的对象：套件共享的断言与任务自己的断言对象相同时，以任务的为准。"""
        return f"event:{self.event}" if self.event else f"metric:{self.metric}"


class RunExpect(BaseModel):
    """一轮预期的终态（doc/cli-func-cases.html §4「预期内的失败」）：满足就不算错误。

    error_kind 是候选（任一即可，空 = 不限）；message 是错误消息里应有的一段话。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed", "cancelled", "interrupted"] = "succeeded"
    error_kind: list[str] = Field(default_factory=list)
    message: str | None = None

    def matches(self, status: str, error_kind: str | None, message: str | None) -> bool:
        if status != self.status:
            return False
        if self.error_kind and (error_kind or "") not in self.error_kind:
            return False
        return self.message is None or self.message in (message or "")


class SimulatedUser(BaseModel):
    """模拟用户（doc/ask-user-question-design.html §12）：智能体用 ask_user_question 提问时，
    由评测侧的模型扮演用户、只依据 brief 作答（agent/user.py）。

    ★ brief 是用户心里的**完整**需求，Agent 看不到 —— 指令里只给一句话，它不问就无从知道。
      没有 user 的任务：问题按推荐项回答（不带答案的批准，服务端填推荐项），与原来的行为一致。
    """

    brief: str


class Turn(BaseModel):
    """同一会话里的后续一轮（多轮任务）：上一轮到终态、已触发的故障动作都做完之后再发。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    #: 发这一轮之前先等多久（如等闲置回收）
    wait_s: float = 0.0


#: 故障动作（doc/sandbox-eng-cases.html §4「运行中的动作」）
ActionKind = Literal[
    "cancel_run",  # 取消触发它的那个 run
    "kill_server",  # 强删持有这个 run 的 server Pod（崩溃）
    "restart_server",  # 滚动部署 server（停机交接）
    "pause_server",  # 暂停持有这个 run 的 server 进程（假死），trial 结束前一定恢复
    "resume_server",
    "delete_sandbox",  # 删会话的沙箱 / 会话 Pod：默认强删，args.graceful 为真时优雅删除
    "block_sandbox",  # 阻断进入沙箱的流量（server → execd / bridge 断网），trial 结束前一定恢复
    "unblock_sandbox",
    "scale_lifecycle",  # 把原生沙箱的 OpenSandbox lifecycle server 缩放到 args.replicas，结束前恢复
    "kill_cli",  # 在会话 Pod 里结束这个会话的 CLI 进程（bridge 拉起的 adapter）
]


class Trigger(BaseModel):
    """按事件触发（不按绝对时间）：会话事件流里第 nth 条满足条件的事件。

        event   事件类型，如 tool.started / run.resumed / run.finished
        tool    事件 data.name 等于它
        mark    命令（参数里的 command）调用了 marks/mark.sh <mark>（计数脚本，见套件的
                _shared/files）。不看工具名：CLI 的 Shell 工具名是命令本身的第一行

    ★ event 为 tool.started、带 mark 时，同一个调用的 tool.updated 也算（按 call_id 只算一次）：
      CLI 一开始只报工具名，命令随后用 tool.updated 补全。
    """

    model_config = ConfigDict(extra="forbid")

    event: str
    tool: str | None = None
    mark: str | None = None
    nth: int = 1


class Action(BaseModel):
    """运行中的一个动作：触发后等 after_s 再执行。

    when 为空 = trial 一开始（发第一轮之前）就执行。deadline_s：从 trial 开始算，到时还没触发就
    照样执行（兜底 —— 比如「接管后恢复假死的实例」，单副本时永远等不到接管）。

    ★ 触发条件的键叫 when 不叫 on：YAML 1.1 把 on 读成布尔值 true，写成 on 会被静默丢掉、
      动作在开局就执行（踩过）。未知的键一律报错（extra=forbid），写错了早发现。
    """

    model_config = ConfigDict(extra="forbid")

    do: ActionKind
    when: Trigger | None = None
    after_s: float = 0.0
    deadline_s: float | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    desc: str = ""


class Task(BaseModel):
    id: str  # <套件>/<任务>
    suite: str
    category: Literal["cli", "code", "data", "tool", "dialog", "security", "eng"] = "cli"
    tags: list[str] = Field(default_factory=list)
    instruction: str
    #: 初始工作区：相对路径 → 内容（来自任务的 files/ 与套件的 _shared/files/；
    #: 不是文本的原样保留字节）
    files: dict[str, str | bytes] = Field(default_factory=dict)
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
    #: 模拟用户：Agent 提问时按它回答（需求澄清评测，tags 里用 ambiguous / clear 标成对的两题）
    user: SimulatedUser | None = None
    #: 同一会话里接着发的几轮（第一轮是 instruction）
    turns: list[Turn] = Field(default_factory=list)
    #: 运行中注入的故障（需要集群操作权限：kubectl，暂停进程还要 ssh 到节点）
    actions: list[Action] = Field(default_factory=list)
    #: 资源回收检查（指标 sandboxes_left）：这么多秒内应当回收干净。回收什么看 reclaim_scope：
    #:   thread     结束后删除会话：原生沙箱 Pod 都消失；CLI 会话的 bridge 都停了（会话 Pod 按人
    #:              共用，还有别的会话时 Pod 不退役）
    #:   subagents  不删父会话：子会话（ephemeral）的 bridge 都停了
    #:   image      会话 Pod 起不来的用例：用 CLI 镜像（agent_patch 里的 cli.image）的 Pod 都消失
    reclaim_within_s: float | None = None
    reclaim_scope: Literal["thread", "subagents", "image"] = "thread"
    #: 各轮预期的终态（键 = 第几轮，1 是 instruction）。没写的轮预期成功。满足的失败不算错误
    run_outcomes: dict[int, RunExpect] = Field(default_factory=dict)
    #: 自动标题检查：建会话时不带标题（带了就算用户起的，自动生成不覆盖），各轮结束后这么多秒内
    #: 标题来源应变成 generated 或 fallback（指标 title_pending）
    title_within_s: float | None = None
    #: 已知缺陷（文档里的编号与一句话）：结果照常进报告，但不进硬门槛；意外通过时报告里提示
    known_defect: str | None = None
    #: 用例自己会让沙箱出错（如把内存打满）：沙箱丢失、结果未知是预期内的，不算平台错误
    sandbox_errors_expected: bool = False

    @field_validator("approval_policy")
    @classmethod
    def _policy(cls, v: str | None) -> str | None:
        return _check_policy(v)

    def injects_faults(self) -> bool:
        """注入了故障（或用例自己制造故障）：沙箱丢失、结果未知是预期内的工具结果，不算平台错误。"""
        return self.sandbox_errors_expected or any(a.do != "cancel_run" for a in self.actions)

    def needs_cluster(self) -> bool:
        return self.injects_faults() or self.reclaim_within_s is not None


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
    #: 不跑带这些标签的任务（如 needs-2-replicas：测试环境只有一个 server 副本时）
    tags_none: list[str] = Field(default_factory=list)
    limit: int | None = None
    #: 平台接口连不上时 GET 重试多久（秒）。故障集要盖过 server Pod 重建 + 端口转发重连
    api_retry_s: float = 60.0
    trials: int = 3
    concurrency: int = 3
    #: approve_all：能力评测，不让审批成为变量（次数与等待仍计入指标）
    approval_policy: str = "approve_all"
    #: 扮演模拟用户的模型（OpenAI 兼容接口，同裁判的地址与密钥）。有任务带 user 时必填
    user_model: str | None = None
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
    #: 任务标了已知缺陷：不进硬门槛
    known_defect: str | None = None
    #: 注入的故障动作：[{do, desc, at_s, ok, detail}]
    actions: list[dict[str, Any]] = Field(default_factory=list)
