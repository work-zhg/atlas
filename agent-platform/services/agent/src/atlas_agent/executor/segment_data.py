"""Segment：Run 执行器交给运行时的「这一段要跑什么」。

★ 不放在 domain：它带着会话的 ORM 行和这一轮解析好的模型端点。纯计算层的 runner 不认识它，
  只认 runner.SegmentInput 这个只读协议（runner 真正读的那几个字段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_engine.contracts.messages import StoredMessage
from atlas_engine.contracts.spec import AgentSpec

if TYPE_CHECKING:
    from ..db.models import Thread
    from .run_models import RunModels


@dataclass(frozen=True)
class Segment:
    """一段要跑什么（c4-model/agent/native-runtime/code-native-runtime-turn.html）。

    由 Run 执行器从库里还原（SegmentLoader）：智能体配置快照、本轮输入、历史、是否续跑、接着
    哪个序号，以及挂起现场（waiting_on、decisions）、审批模式和跨段 state。一个对象交到底，
    原生和 CLI 两种运行时都只认它，runner 不再收十几个平铺参数。

    原先叫 PreparedRun —— 从库里还原出的「任务是什么」。

    history_upto: 本轮起始时历史末尾的时间 —— 若本轮触发压缩，摘要覆盖到
    这里。★ 必须随任务传递而不是存在执行器实例上：执行器是进程级单例，
    同时跑多个 run，实例属性会互相覆盖。
    """

    spec: AgentSpec
    input_content: Any
    thread: Thread
    #: 本次执行用的配置版本。子 run 沿用**同一个** —— 子智能体的配置就住在
    #: 这份快照的 subagents 里，换一份等于换掉了「当时用的哪份配置」。
    agent_version_id: UUID | None = None
    #: 本轮之前的历史（摘要边界之后的部分），存储格式：转成框架的消息是原生运行时的事
    history: list[StoredMessage] = field(default_factory=list)
    #: 压缩摘要：覆盖边界之前的早期对话。None = 没压缩过
    summary: str | None = None
    history_upto: datetime | None = None
    #: 事件序号的起点 = 已发出的最后一个 seq。一个 run 分多段执行时，后一段
    #: 必须接着前一段往下数（契约规则 2 的「无空洞」是跨段成立的）。
    #: 0 = 全新的 run。
    start_seq: int = 0
    #: True = 这个 run 之前已经跑过一段，本次是续跑。本轮输入早已在 history
    #: 里，不能再 append 一遍（那会让模型看到同一个问题问了两次）。
    resume: bool = False
    #: 这个 run 在之前几段里已经花掉的 token。max_total_tokens 是跨段累计的。
    prior_tokens: int = 0
    #: 这个 run 在委派树里的深度：主 run 是 0，子 run 是 1。进 TraceEvent.depth，
    #: 前端据此把子智能体的过程渲染进卡片而不是主对话。
    base_depth: int = 0
    #: 本段是接管来的（横向扩展设计 §5）：None = 正常领取；"handoff" / "expired" = 上一个
    #: 执行器停机交接 / 租约过期。acp 据此只 attach 不重开一轮。
    takeover: str | None = None
    #: acp：会话的接续信息（cli_session 表，段开始时读出）—— CLI 自己的会话 id（open 时恢复），
    #: 以及已处理到的 bridge 实例与序号（§5.2，接管时从这里 attach 补发）
    cli_session_id: str | None = None
    cli_bridge_instance: str | None = None
    cli_acked_seq: int = 0
    #: 续跑时交回图的挂起现场（contracts/suspension.py）：上一段还在等的调用
    #: （run.waiting_on，已剪掉历史里有结果的），和 server 查好的结果。首段都为空。
    waiting_on: dict[str, Any] = field(default_factory=dict)
    #: approval → ApprovalOutcome（结构体）；subagent → 结论文本
    decisions: dict[str, Any] = field(default_factory=dict)
    #: 上一段落库的图 state（run_state 表，如 todos），作为图的输入交回。全新的 run 为空
    run_state: dict[str, Any] = field(default_factory=dict)
    #: 本段开始时的审批模式（run.approval_mode；doc/native-approval-mode-design.html）。
    #: native 交给图；acp 子智能体据此与自己的 permission_mode 取更严格的
    approval_mode: str | None = None
    #: 本轮用户消息的时间。Agent 在这一轮写入记忆时用它作为「信息成立的时间」（记忆设计 §6.4）
    input_at: datetime | None = None
    #: 会话的上下文状态（thread_context）：压缩过几次（记忆据此重建常驻快照）、
    #: 自动压缩连续失败了几次（熔断用）
    compact_count: int = 0
    compaction_failures: int = 0
    #: 这一轮的模型端点与目录（CapabilityAssembly.with_models 填）。None = 还没解析，按直连处理
    models: RunModels | None = None

    @property
    def thread_id(self) -> UUID:
        return self.thread.thread_uuid
