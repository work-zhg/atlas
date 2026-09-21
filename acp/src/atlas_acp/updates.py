"""session/update 通知的判别联合 —— translate 的全部输入。

判别键是 `update.sessionUpdate`。与 ACP 的线上形态一致：

    {"sessionId": "...", "update": {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": "..."}}}

★ 未知判别值不报错，解析成 `UnknownUpdate` 保留原始载荷。
  协议演进时旧 server 遇到新 update 类型不该把 run 弄失败（acp 详设 §06）——
  调用方据此只记一条警告与一个计数，事件一条不产。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentMessageChunk",
    "AgentThoughtChunk",
    "ContentBlock",
    "PlanEntry",
    "PlanUpdate",
    "SessionUpdate",
    "SessionUpdateNotification",
    "ToolCallStart",
    "ToolCallUpdate",
    "UnknownUpdate",
    "parse_update",
]

_CFG = ConfigDict(populate_by_name=True, extra="allow")


class ContentBlock(BaseModel):
    """ACP 的内容块。Atlas 只取 text —— 其余类型（image/audio）本期不渲染。"""

    model_config = _CFG

    type: str = "text"
    text: str = ""


class AgentMessageChunk(BaseModel):
    """模型正文的流式增量。"""

    model_config = _CFG

    session_update: Literal["agent_message_chunk"] = Field(alias="sessionUpdate")
    content: ContentBlock = Field(default_factory=ContentBlock)


class AgentThoughtChunk(BaseModel):
    """模型思考过程的流式增量。

    ★ 与正文**分开**是语义要求，不是分类洁癖：思考混进 message.delta 会被
      拼进最终 assistant 消息，用户看到一段突然冒出来、与回答不连贯的话。
    """

    model_config = _CFG

    session_update: Literal["agent_thought_chunk"] = Field(alias="sessionUpdate")
    content: ContentBlock = Field(default_factory=ContentBlock)


class ToolCallStart(BaseModel):
    """CLI 发起一次工具调用。"""

    model_config = _CFG

    session_update: Literal["tool_call"] = Field(alias="sessionUpdate")
    #: 与 tool_call_update 配对的键 —— 原样带给前端，前端靠它合并行。
    tool_call_id: str = Field(default="", alias="toolCallId")
    #: 人类可读的标题（CLI 侧的措辞）。kind 是粗分类（read/edit/execute…）。
    title: str = ""
    kind: str = ""
    raw_input: Any = Field(default=None, alias="rawInput")


class ToolCallUpdate(BaseModel):
    """工具调用的状态推进。

    status 为 in_progress 时**不产事件** —— 前端的工具行已由 tool_call
    创建，中间态只会让它闪烁（acp 详设 §06 只列了 completed / failed）。
    """

    model_config = _CFG

    session_update: Literal["tool_call_update"] = Field(alias="sessionUpdate")
    tool_call_id: str = Field(default="", alias="toolCallId")
    status: str = ""
    title: str = ""
    content: list[ContentBlock] = Field(default_factory=list)
    raw_output: Any = Field(default=None, alias="rawOutput")

    def output_text(self) -> str:
        """把内容块拼成一段文本 —— 平台的 tool.completed 只认字符串结果。"""
        return "".join(block.text for block in self.content if block.text)


class PlanEntry(BaseModel):
    model_config = _CFG

    content: str = ""
    #: pending / in_progress / completed —— 与平台 TodoStatus 恰好同名同义。
    status: str = "pending"
    priority: str = ""


class PlanUpdate(BaseModel):
    """CLI 的计划列表。★ 全量快照语义，与平台 todos.updated 一致。"""

    model_config = _CFG

    session_update: Literal["plan"] = Field(alias="sessionUpdate")
    entries: list[PlanEntry] = Field(default_factory=list)


class UnknownUpdate(BaseModel):
    """判别值不认识 —— 保留原始载荷，交调用方记警告与计数。"""

    model_config = _CFG

    session_update: str = Field(default="", alias="sessionUpdate")
    raw: dict[str, Any] = Field(default_factory=dict)


SessionUpdate = (
    AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallUpdate | PlanUpdate | UnknownUpdate
)

_BY_KIND: dict[str, type[BaseModel]] = {
    "agent_message_chunk": AgentMessageChunk,
    "agent_thought_chunk": AgentThoughtChunk,
    "tool_call": ToolCallStart,
    "tool_call_update": ToolCallUpdate,
    "plan": PlanUpdate,
}


def parse_update(payload: dict[str, Any]) -> SessionUpdate:
    """`update` 载荷 → 判别联合的一支。未知判别值 → UnknownUpdate，不抛。"""
    kind = str(payload.get("sessionUpdate") or payload.get("session_update") or "")
    model = _BY_KIND.get(kind)
    if model is None:
        return UnknownUpdate(sessionUpdate=kind, raw=dict(payload))
    return model(**payload)  # type: ignore[return-value]


class SessionUpdateNotification(BaseModel):
    """session/update 通知的 params。"""

    model_config = _CFG

    session_id: str = Field(default="", alias="sessionId")
    update: dict[str, Any] = Field(default_factory=dict)

    def parsed(self) -> SessionUpdate:
        return parse_update(self.update)
