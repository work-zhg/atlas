"""从 ``session/update`` 的原始 JSON 里读取控制信号（Bridge 设计 §3.4 · §6.2）。

★ 直接读原始 dict，不经模型往返：bridge 转交给 server 的永远是收到的原始 JSON，
  它只在需要控制信号时读几个字段（代码设计 §3「不经模型往返」）。

★ agent 是不可信的输入（§8.3）：形状不对时返回 None，不抛异常，由调用方决定怎么处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "TOOL_FINISHED",
    "TOOL_UPDATE_KINDS",
    "ToolStatus",
    "tool_status",
    "update_kind",
]

#: 这两种 update 带着工具调用的状态
TOOL_UPDATE_KINDS = frozenset({"tool_call", "tool_call_update"})
#: 工具调用的终态（v1：completed / failed；v2 另有 cancelled）
TOOL_FINISHED = frozenset({"completed", "failed", "cancelled"})


def _update(params: Any) -> Mapping[str, Any] | None:
    if not isinstance(params, Mapping):
        return None
    update = params.get("update")
    return update if isinstance(update, Mapping) else None


def update_kind(params: Any) -> str | None:
    """``session/update`` 通知的 params → ``sessionUpdate`` 的值（如 ``agent_message_chunk``）。"""
    update = _update(params)
    kind = update.get("sessionUpdate") if update is not None else None
    return kind if isinstance(kind, str) else None


@dataclass(frozen=True, slots=True)
class ToolStatus:
    tool_call_id: str
    #: None = 这条更新没有带状态（v1 的 tool_call_update 只带变化的字段）
    status: str | None

    @property
    def finished(self) -> bool:
        return self.status in TOOL_FINISHED


def tool_status(params: Any) -> ToolStatus | None:
    """工具调用类 update → (toolCallId, status)；其它 update 或形状不对 → None。

    ★ v1 的 ``tool_call`` 省略 status 时默认为 pending（规范），这里如实返回 None，
      由调用方按「未开始」处理。
    """
    update = _update(params)
    if update is None or update.get("sessionUpdate") not in TOOL_UPDATE_KINDS:
        return None
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return None
    status = update.get("status")
    return ToolStatus(call_id, status if isinstance(status, str) else None)
