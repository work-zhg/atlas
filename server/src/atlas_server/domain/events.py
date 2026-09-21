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
    TITLE_GENERATED = "thread.title_generated"  # 文档 §8


#: run 的终止事件。SSE 发送后即关闭连接。
TERMINAL_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.RUN_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    }
)


class TraceEvent(BaseModel):
    model_config = {"frozen": True}

    seq: int = Field(ge=1, description="单调递增，= SSE 的 id，断线重连的游标")
    run_id: UUID
    ts: datetime
    type: EventType
    depth: int = Field(default=0, ge=0, description="0=主 agent，1=子 agent，前端缩进用")
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENTS

    def to_sse(self) -> str:
        """序列化为 SSE 帧。id 写 seq，浏览器断线重连自动带 Last-Event-ID。"""
        payload = self.model_dump_json()
        return f"id: {self.seq}\nevent: {self.type.value}\ndata: {payload}\n\n"


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

    ★ 住在契约旁边，且是**全 run 唯一**的一个实例：前端靠 seq 去重与补齐，
      两个工厂各自从 1 数会让断线重连拿到错乱的历史。native 与 acp 两个
      runtime 共用它，正是为了这条纪律不分叉。
    """

    def __init__(self, run_id: UUID, clock: Callable[[], datetime]) -> None:
        self._run_id = run_id
        self._clock = clock
        self._seq = 0

    def make(
        self, type_: EventType, data: dict[str, Any] | None = None, depth: int = 0
    ) -> TraceEvent:
        self._seq += 1
        return TraceEvent(
            seq=self._seq,
            run_id=self._run_id,
            ts=self._clock(),
            type=type_,
            depth=depth,
            data=data or {},
        )
