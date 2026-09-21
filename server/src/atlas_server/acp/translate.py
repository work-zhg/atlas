"""ACP → TraceEvent：acp 与前端之间唯一的语义换算点（acp 详设 §06）。

验收标准来自包结构设计：**web 一行不改**。如果前端需要为 acp 加分支，
说明这张表没翻干净 —— 该改的是这里，不是前端。

## 为什么返回「意图」而不是 TraceEvent

详设里写的是 `(update, seq工厂) → list[TraceEvent]`。落地时改成返回
`(EventType, data)` 二元组：

  · seq 的单调无空洞是**全 run 唯一**的契约（events.py 规则 2），
    由 _EventFactory 独占。翻译层拿不到工厂，就不可能破坏它。
  · §06 的表本身就是「一种输入 → 一组 (类型, data)」，意图与表 1:1，
    单测可以逐行照抄而不用先造一个假工厂。

调用方（AcpRuntime）拿到意图后统一 `events.make(type, data)`。

## data 的形状必须与 native 逐字段对齐

前端的 reducer 按 `data` 的字段名取值，形状差一个字段就是「要加分支」。
所以工具调用复用 domain.translator 的 `_preview`，用量复用
normalize_usage 的键名 —— 不在这里另造一套。
"""

from __future__ import annotations

import logging
from typing import Any

from atlas_acp.types import STOP_REASONS, AcpUsage
from atlas_acp.updates import (
    AgentMessageChunk,
    AgentThoughtChunk,
    PlanUpdate,
    SessionUpdate,
    ToolCallStart,
    ToolCallUpdate,
    UnknownUpdate,
)

from ..domain.events import EventType
from ..domain.translator import preview_args

logger = logging.getLogger(__name__)

__all__ = ["EventIntent", "translate_crash", "translate_stop", "translate_update"]

#: 一条待加工的事件：(类型, data)。调用方补 seq / run_id / ts / depth。
EventIntent = tuple[EventType, dict[str, Any]]

#: 工具结果进事件流的长度上限，与 native 的 result_preview 同口径。
_RESULT_PREVIEW = 200

#: 只有终态的 tool_call_update 产事件。in_progress 不产 ——
#: 前端的工具行已由 tool_call 建好，中间态只会让它闪烁。
_TOOL_TERMINAL = {
    "completed": EventType.TOOL_COMPLETED,
    "failed": EventType.TOOL_FAILED,
}


def translate_update(update: SessionUpdate) -> list[EventIntent]:
    """一条 session/update → 零或多条事件意图。"""
    match update:
        case AgentMessageChunk():
            text = update.content.text
            # 空块不产事件：CLI 的流里常有只带 signature 的占位块，
            # 放行会让前端收到一串空 delta。
            return [(EventType.MESSAGE_DELTA, {"text": text})] if text else []

        case AgentThoughtChunk():
            # ★ 思考走 thinking.delta，**不是** message.delta。
            #
            #   详设 §06 写的是「message.delta（thinking 块）」，但 message.delta
            #   的 data 只有 {text}，无从区分思考与正文 —— 混进去会被拼成
            #   assistant 的最终消息，用户看到一段与回答不连贯的自言自语。
            #   THINKING_DELTA 这个类型本就是为它保留的（native 侧因网关不透传
            #   思考文本而一直空置），前端的事件联合里已有，不需要改一行。
            text = update.content.text
            return [(EventType.THINKING_DELTA, {"text": text})] if text else []

        case ToolCallStart():
            args = update.raw_input if update.raw_input is not None else {}
            return [
                (
                    EventType.TOOL_STARTED,
                    {
                        # toolCallId 原样带过去 —— 前端靠它把 started 与
                        # completed 合并成一行。
                        "call_id": update.tool_call_id,
                        "name": update.title or update.kind,
                        "args": args,
                        "args_preview": preview_args(args),
                    },
                )
            ]

        case ToolCallUpdate():
            event_type = _TOOL_TERMINAL.get(update.status)
            if event_type is None:
                return []
            text = update.output_text()
            return [
                (
                    event_type,
                    {
                        "call_id": update.tool_call_id,
                        "name": update.title,
                        "status": "error" if event_type is EventType.TOOL_FAILED else "success",
                        "result": text,
                        "result_preview": text[:_RESULT_PREVIEW],
                    },
                )
            ]

        case PlanUpdate():
            # 全量快照语义与平台 todos.updated 一致（events.py 规则 1），
            # ACP 的 status 取值恰好与平台 TodoStatus 同名同义。
            return [
                (
                    EventType.TODOS_UPDATED,
                    {
                        "todos": [
                            {"content": e.content, "status": e.status} for e in update.entries
                        ]
                    },
                )
            ]

        case UnknownUpdate():
            # ★ 不翻成 error。协议演进时旧 server 遇到新 update 类型不该把
            #   run 弄失败 —— 记一条警告，调用方顺带计数，事件一条不产。
            logger.warning("未知的 session/update 类型：%s", update.session_update or "(空)")
            return []

    return []


def translate_stop(stop_reason: str, usage: AcpUsage | None = None) -> list[EventIntent]:
    """prompt 响应的 StopReason（+ 用量）→ 终止事件意图。

    ★ 撞上限不是失败：max_tokens / max_turn_requests 映射成 run.finished
      加一个 stop_reason 附加字段。翻成 run.failed 的话，「任务做完了但
      被截断」与「真的出错了」在前端长得一模一样 —— 这正是 subagent 设计里
      记着要还的那条改进。
    """
    out: list[EventIntent] = []
    if usage is not None:
        out.append((EventType.USAGE_UPDATED, usage.model_dump()))

    if stop_reason == "cancelled":
        out.append((EventType.RUN_CANCELLED, {"stop_reason": stop_reason}))
        return out

    if stop_reason == "refusal":
        # 拒答不是故障：重试无用，把 CLI 自己的说明原样展示（§13.1）。
        out.append(
            (
                EventType.RUN_FAILED,
                {
                    "error_kind": "model_refused",
                    "message": "模型拒绝作答",
                    "retryable": False,
                    "stop_reason": stop_reason,
                },
            )
        )
        return out

    data: dict[str, Any] = {
        "total_tokens": usage.total_tokens if usage else 0,
        "text_len": 0,  # 由调用方按累计正文长度覆盖
        "stop_reason": stop_reason,
    }
    if stop_reason not in STOP_REASONS:
        # 认不出的终了原因照样收尾 —— 让 run 挂在 running 比标错更糟。
        logger.warning("未知的 StopReason：%s", stop_reason)
    out.append((EventType.RUN_FINISHED, data))
    return out


def translate_crash(detail: str = "") -> list[EventIntent]:
    """bridge 自报的 adapter 崩溃 → 可重试的 run.failed。

    ★ 崩溃是故障不是拒答，retryable=True —— 用户重试一次通常就好了，
      而 model_refused 重试一百次也一样。
    """
    return [
        (
            EventType.RUN_FAILED,
            {
                "error_kind": "runtime_crashed",
                "message": detail or "CLI adapter 进程异常退出",
                "retryable": True,
            },
        )
    ]
