"""图的 state schema：在 demo/suspend-resume 的 waiting_on / decisions 之上多一个 mode。

    mode        运行中的审批模式。开始时由执行器从 run 行交进来；计划获批时由 PermissionMiddleware
                改写成用户选的执行模式；挂起时和消息、waiting_on 一起落库，续跑时原样交回
    waiting_on  tool_call_id → {"reason": "approval", "token": ...}（同 demo/suspend-resume）
    decisions   token → 结构化的审批结果 {"status": "approved", "mode": "auto"}。
                计划审批要带回「用户选的执行模式」，所以不再是一个字符串
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired

from langchain.agents.middleware.types import AgentState, OmitFromOutput

Mode = Literal["plan", "manual", "auto"]
MODE_LABEL: dict[str, str] = {"plan": "规划", "manual": "手动", "auto": "自动"}


def merge_waits(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """按 tool_call_id 合并，值为 None 表示删除（为什么要 reducer 见 demo/suspend-resume）。"""
    merged = {**(old or {}), **(new or {})}
    return {k: v for k, v in merged.items() if v is not None}


class PermissionState(AgentState):
    mode: NotRequired[Mode]
    waiting_on: NotRequired[Annotated[dict[str, dict[str, str]], merge_waits]]
    decisions: NotRequired[Annotated[dict[str, dict[str, Any]], OmitFromOutput]]
