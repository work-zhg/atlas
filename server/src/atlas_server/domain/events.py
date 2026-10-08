"""★ TraceEvent 契约 —— 前端依赖的稳定边界（文档 §4.2）。

前端只认这份 schema，永远不接触 kernel / LangGraph 的内部结构。
内核将来被替换，只要 translator 还能产出这些事件，web 一行都不用动。

三条契约规则，破了就是 breaking change：
  1. TODOS_UPDATED 是全量快照（write_todos 语义是整表替换），前端不做 diff 合并
  2. seq 在单个 run 内从 1 严格递增无空洞，前端靠它去重与补齐
  3. data 只增字段不删不改类型；删字段或改语义必须走新 EventType
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    #: 这一段跑完了，但 run 还没结束 —— 在等子智能体的结果。
    #:
    #: ★ **不是终止事件**。SSE 不关闭，前端继续挂着（`run.status` 仍是未完成，
    #:   stream() 因此走 tail 分支而不是「回放完就关」）。收到它应当显示
    #:   「等待子智能体」，而不是把这一轮标成结束。
    #:
    #: ★ 为什么必须是事件而不是日志：委派可能要等一小时。这段时间里事件流
    #:   一个字都不产出 —— 没有这条，用户看到的是一个卡住的界面，分不清
    #:   「在等」和「挂了」。
    RUN_SUSPENDED = "run.suspended"

    # ⚠️ THINKING_DELTA 目前无法通过 litellm 网关产出：
    #    实测 thinking.display="summarized" 不透传，返回的 thinking block
    #    文本恒为空（仅带 signature）。可用的只有"是否思考过"与 thinking_tokens
    #    计数，两者都随 USAGE_UPDATED 上报。保留该类型以备网关放开后启用。
    THINKING_DELTA = "thinking.delta"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"

    TODOS_UPDATED = "todos.updated"  # ★ 全量快照，非增量
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_STEP = "subagent.step"
    SUBAGENT_FINISHED = "subagent.finished"

    FILE_WRITTEN = "file.written"
    FILE_DELETED = "file.deleted"

    USAGE_UPDATED = "usage.updated"
    APPROVAL_REQUIRED = "approval.required"

    CONTEXT_COMPACTED = "context.compacted"  # 文档 §7.5，必须对用户可见
    #: acp：CLI 会话没能恢复，已新建（acp 详设 §10）。
    #: ★ 必须是**事件**而不是日志：静默新建的表现是用户以为接着上次继续，
    #:   CLI 实际从零开始 —— 它会重新读一遍代码、重新问一遍已回答过的问题，
    #:   而事件流上看不出任何异常。这是最坏的失败形态。
    SESSION_LOST = "session.lost"
    #: acp：这一轮 CLI 实际生效的权限模式（requested / effective / degraded）。
    #: ★ 必须让用户看见：auto 可能被 adapter 降级，不说的话用户以为是 auto，
    #:   实际每条命令都在请示。
    AGENT_MODE = "agent.mode"
    TITLE_GENERATED = "thread.title_generated"  # 文档 §8

    #: 模型读了某个技能的 SKILL.md —— 渐进式披露下「选中了这个技能」唯一可观测的信号
    #: （技能 / MCP 设计 §7.4）。data: {slug, version, call_id}
    SKILL_LOADED = "skill.loaded"
    #: 引用的技能这一轮没有装上（紧急下架等）。data: {slug, version, reason}
    #: ★ 必须是事件：静默少一个技能的表现是模型「照常」做事，只是不按技能做。
    SKILL_SKIPPED = "skill.skipped"
    #: MCP 工具的定义与 agent 保存时记录的不一致，或尚未复核（§9）。
    #: data: {tool, server, old_digest, new_digest, action}
    MCP_TOOL_DRIFT = "mcp.tool_drift"


#: run 的终止事件。SSE 发送后即关闭连接。
#:
#: ★ RUN_SUSPENDED 刻意不在其中：它是「这一段结束」，不是「这一轮结束」。
#:   放进来的话 SSE 会在委派开始的那一刻关闭，而真正的结论要等一小时后的
#:   下一段 —— 用户永远等不到它。
TERMINAL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.RUN_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    }
)

#: 「这一段到此为止」的事件。执行器据此收尾，但不把 run 判死。
SEGMENT_END_EVENTS: frozenset[EventType] = TERMINAL_EVENTS | {EventType.RUN_SUSPENDED}


class TraceEvent(BaseModel):
    model_config = {"frozen": True}

    seq: int = Field(ge=1, description="run 内单调递增")
    #: 会话内单调递增 —— 契约规则 2 的**新口径**，thread 流的游标。
    #:
    #: ★ 0 = 尚未分配。分配发生在发布时（EventRelay.publish），因为
    #:   thread 级序号有多个并发生产者（并行委派的几个子 run 同时写同一条
    #:   流），只能由一个共享计数器给出 —— EventFactory 在进程内数不出来。
    #:
    #: ★ 过渡期两个序号并存：前端还在用 run 级 seq 做游标，S4 才切过来。
    #:   切完之后 seq 只剩诊断价值（「这是那个 run 的第几个事件」）。
    thread_seq: int = Field(default=0, ge=0, description="thread 内单调递增，thread 流的游标")
    run_id: UUID
    ts: datetime
    type: EventType
    #: ★ 语义 = 这个 run 在**委派树**里的深度：0 = 主 agent，1 = 子智能体。
    #:   委派深度结构性封顶 1，所以取值只有 {0, 1}。
    #:
    #: ★ 曾经量的是 LangGraph 子图的命名空间深度。委派改成「子会话 = thread +
    #:   独立子 run」之后图内不再编译子图，那个口径恒为 0 —— 于是前端按 depth
    #:   缩进的逻辑成了死代码。现在它重新有值：子 run 产出的事件带 depth=1，
    #:   前端据此把它们渲染进子智能体卡片而不是主对话。
    depth: int = Field(default=0, ge=0, description="0=主 agent，1=子 agent，前端缩进用")
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS

    @property
    def ends_segment(self) -> bool:
        """这一段到此为止（终态**或**挂起）。执行器的收尾判据。"""
        return self.type in SEGMENT_END_EVENTS

    def stamped(self, thread_seq: int) -> TraceEvent:
        """盖上会话级序号。frozen，所以返回新对象。"""
        return self.model_copy(update={"thread_seq": thread_seq})

    def to_sse(self, *, thread_cursor: bool = False) -> str:
        """序列化为 SSE 帧。id 写游标 —— 浏览器断线重连自动带 Last-Event-ID。

        thread_cursor=True 时 id 写 `thread_seq`（会话流的游标），否则写 `seq`
        （run 流的游标）。

        ★ 两个端点的游标口径必须各自自洽。混了的后果是重连时游标被拿去另一个
          序号体系里比较 —— 数值相近（都是小整数），于是不报错，只是补发的
          范围整个错位：要么重复一大段，要么静默丢一大段。
        """
        payload = self.model_dump_json()
        cursor = self.thread_seq if thread_cursor else self.seq
        return f"id: {cursor}\nevent: {self.type.value}\ndata: {payload}\n\n"


class Answer:
    """助手正文，**按轮分段**。

    一个 run 里模型往往说好几轮：先说一句「我去委派一下」再调工具，拿到
    结果之后再说结论。早先这些文本被 `"".join` 成一整条，于是过程性发言与
    最终回答粘在一起 —— 用户看到的是「I'll delegate the file creation to
    the cli-hand subagent, then read it back myself.已完成。…」，分不出
    哪句才是结论（中英文混在一起更明显，因为两轮是分开生成的）。

    ★ 段的边界是**工具调用**，不是段落或换行：模型一旦调工具，这一轮就说完
      了，下一条 delta 属于下一轮。这是唯一不依赖文本内容的判据。

    ★ 落地形态就是 Anthropic 的 content blocks —— 一轮一个 text block。
      message.content 本来就是 blocks 数组，不用改表结构，刷新后仍然分得开。

    ★ native 与 acp 共用：两边都有「一轮多次发言」这件事，粘连也是同一种。
    """

    def __init__(self) -> None:
        self._blocks: list[str] = [""]

    @property
    def index(self) -> int:
        """当前段的序号 —— 进 message.delta，前端据此边流边分段。"""
        return len(self._blocks) - 1

    def append(self, delta: str) -> None:
        self._blocks[-1] += delta

    def seal(self) -> None:
        """这一轮说完了（调工具了），下一条 delta 另起一段。

        ★ 当前段为空就不开新段：模型连着调两个工具、或一上来就调工具，
          中间并没有话 —— 那样会插进一串空 block。
        """
        if self._blocks[-1]:
            self._blocks.append("")

    @property
    def text(self) -> str:
        """全文。给 partial_text 这类「不分段」的口径用。"""
        return "".join(self._blocks)

    def content(self) -> list[dict[str, str]]:
        """message.completed 的 content —— 空段丢掉。

        ★ 一段都没有时返回一个空 text block，而不是空数组：下游（落库、
          前端 textOf）一直假定至少有一块，空数组会让「模型只调工具没说话」
          这种 run 在界面上变成一条没有主体的消息。
        """
        blocks = [{"type": "text", "text": b} for b in self._blocks if b]
        return blocks or [{"type": "text", "text": ""}]


class EventFactory:
    """集中管理 seq —— 契约规则 2：单个 run 内从 1 严格递增无空洞。

    ★ 住在契约旁边，且**一个 run 的同一时刻只有一个实例**：前端靠 seq 去重
      与补齐，两个工厂同时各自数会让断线重连拿到错乱的历史。native 与 acp
      两个 runtime 共用它，正是为了这条纪律不分叉。

    ★ `start_seq` 不是这条纪律的例外，而是它的延伸。一个 run 可以分多段执行
      （委派挂起后续跑），段之间隔着一次进程重启都有可能 —— 后一段必须从
      `run.last_seq` 接着往下数。「无空洞」因此是跨段成立的，而不是每段
      从 1 重来。默认 0 = 全新的 run。

    ★ `base_depth` 是这个 run 在**委派树**里的位置：主 run 是 0，子 run 是 1。
      产出事件时与局部深度相加，见 `make` 的说明。
    """

    def __init__(
        self,
        run_id: UUID,
        clock: Callable[[], datetime],
        *,
        start_seq: int = 0,
        base_depth: int = 0,
    ) -> None:
        self._run_id = run_id
        self._clock = clock
        self._seq = start_seq
        self._base_depth = base_depth

    def make(
        self, type_: EventType, data: dict[str, Any] | None = None, depth: int = 0
    ) -> TraceEvent:
        """`depth` 是调用方给的**局部**深度，最终值再叠上这个 run 的 base_depth。

        ★ 两者相加而不是二选一：局部深度来自图内的子图命名空间，base_depth
          来自 run 的父子关系，它们量的是不同的东西。委派模式下图内不编译
          子图（局部恒 0），所以实际取值就是 base_depth —— 但相加这条规则
          让将来真出现图内嵌套时不用改这里。
        """
        self._seq += 1
        return TraceEvent(
            seq=self._seq,
            run_id=self._run_id,
            ts=self._clock(),
            type=type_,
            depth=self._base_depth + depth,
            data=data or {},
        )
