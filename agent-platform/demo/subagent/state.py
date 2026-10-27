"""智能体图的 state schema：在 messages 之外加两个字段。主智能体和 SubAgent 用同一个。

和 demo/suspend-resume 的 SuspendState 是同一个形状，只是 decisions 换成了 results：
那边等的是审批决策，这边等的是子 run 的结论。
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import AgentState, OmitFromOutput


def merge_waits(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """waiting_on 的 reducer：按 tool_call_id 合并，值为 None 表示删除。

    ★ 为什么要 reducer：同一批的几个 task 是并行执行的（每个 tool_call 一个 Send），
      它们在同一步里各写一次 waiting_on。没有 reducer，LangGraph 报 InvalidUpdateError
      （README 实验 1）。
    ★ 为什么不用 operator.add：拿到结论后要把那一项删掉，add 只能加不能删。
    """
    merged = {**(old or {}), **(new or {})}
    return {k: v for k, v in merged.items() if v is not None}


class SubAgentState(AgentState):
    # tool_call_id -> {"reason": "subagent", "token": 子 run id}
    # 不加可见性标注：挂起时要从返回值里取出来落库，续跑时要原样交回来
    waiting_on: NotRequired[Annotated[dict[str, dict[str, str]], merge_waits]]

    # 子 run id -> 交给模型的结论文本
    # 只进不出：续跑时由 server 交进来，不需要再带回去
    results: NotRequired[Annotated[dict[str, str], OmitFromOutput]]
