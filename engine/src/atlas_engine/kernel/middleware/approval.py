"""高风险工具的人工确认（文档 §12.2）。

拦截点是 langchain middleware 的 `awrap_tool_call`，不是 LangGraph 的
`interrupt()` —— 后者要求 checkpointer，而本项目刻意没有第二个状态源
（doc/detail/suspension.html §03）。

★ 等待方式从「在线阻塞」改成「挂起」（§06）。

  原先这里 `await gate.request(...)` 一路等到有人点头或 600s 超时：一个
  asyncio.Task 全程挂着，进程重启即丢。现在问一次 `gate.check(...)`，还没
  结果就返回一个挂起哨兵让本段结束 —— 等待交给数据库，决策到达后续跑。

  两条路在图里的差别只有一处：**挂起时工具还没执行**。续跑时 server 把哨兵
  那条 ToolMessage 整条删掉，于是 tool_use 变成悬空，SuspensionMiddleware
  看到就跳去 tools 节点把它跑掉。委派则相反（结果已有，换掉内容即可）。

等待逻辑由调用方经 `contracts.ApprovalGate` 注入 —— kernel 不知道
Postgres 与 Redis 的存在。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from atlas_engine.contracts import ApprovalGate
from atlas_engine.contracts.suspension import suspend_marker

__all__ = [
    "EXPIRED_RESULT",
    "GATE_FAILED_RESULT",
    "REJECTED_RESULT",
    "ApprovalMiddleware",
    "approval_id_for",
]

#: 被拒绝时回给 agent 的工具结果。★ 不是抛异常 ——
#: 让 agent 知道「这条路被挡了」，它可以换个方案继续（§12.2）。
REJECTED_RESULT = "用户拒绝执行该工具。请换一种方式完成任务，或说明为什么必须使用它。"
EXPIRED_RESULT = "等待人工确认超时，该工具未被执行。"

#: 门禁**自己坏了**时回给 agent 的结果。
#:
#: ★ 与 REJECTED_RESULT 分开的理由，是给模型的信息不一样。「用户拒绝」意味着
#:   意图被否决 —— 正确反应是换方案。「系统故障」意味着意图没人评判过 ——
#:   正确反应是报告故障，而不是绕路。喂错这句话，模型会围着一个不存在的
#:   产品决策打转（真机上它去排查「子智能体为什么静默」，而真因是一列
#:   varchar(128)）。
GATE_FAILED_RESULT = (
    "审批系统故障，该工具未被执行 —— 这不是用户的拒绝，没有人看到过这次请求。"
    "请把这个故障如实报告给用户，不要改用别的方式绕过它。"
)

#: 派生 approval_id 的命名空间。固定值 —— 换了它等于让所有历史审批的 id 变样。
_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def approval_id_for(run_id: str, tool_call_id: str) -> str:
    """从 (run_id, tool_call_id) 派生 approval_id。

    ★ **必须可复现**，这是审批能挂起的前提。

      随机 id（原先是 `uuid4()`）在续跑时会算出一个**新的** id：gate 查不到
      记录 → 当成首次 → 返回 pending → 再挂起一次。**死循环**，而且看起来
      像「批准了但没反应」。

    ★ 为什么这两个值够：tool_call_id 由工具节点生成、落在 message 表里、跨段
      稳定；run_id 让不同 run 之间不会撞（同一个 tool_call_id 在另一个 run 里
      重复出现是可能的）。

    ★ 顺带修掉一个既有隐患：随机 id 在重试路径上会留下孤儿 Approval 行。
    """
    return str(uuid.uuid5(_NS, f"{run_id}:{tool_call_id}"))


class ApprovalMiddleware(AgentMiddleware):
    """对 `require_approval_for` 里的工具，执行前先确认批过了。"""

    def __init__(self, tool_names: frozenset[str], gate: ApprovalGate) -> None:
        super().__init__()
        self._guarded = tool_names
        self._gate = gate

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        name = str(call.get("name", ""))
        if name not in self._guarded:
            return await handler(request)

        from langgraph.config import get_stream_writer

        tool_call_id = str(call.get("id", ""))
        run_id = _run_id_of(request)
        approval_id = approval_id_for(run_id, tool_call_id)
        args = call.get("args") or {}

        state = await self._gate.check(approval_id=approval_id, tool_name=name, args=args)

        if state == "approved":
            return await handler(request)

        # ★ 三者都不放行，但给模型的说法不同 —— 见 GATE_FAILED_RESULT 的注释。
        if state in ("rejected", "expired", "failed"):
            return ToolMessage(
                content={
                    "rejected": REJECTED_RESULT,
                    "expired": EXPIRED_RESULT,
                    "failed": GATE_FAILED_RESULT,
                }[state],
                tool_call_id=tool_call_id,
                name=name,
                status="error",
            )

        # pending：把「需要确认」发出去，然后挂起本段。
        #
        # ★ 事件必须发 —— 前端的弹窗正是由它驱动的。不发的话没有任何人知道有
        #   审批在等，run 会停在 suspended 直到超时判定（等同拒绝）。
        get_stream_writer()(
            {
                "kind": "approval.required",
                "approval_id": approval_id,
                "tool_name": name,
                "args": args,
                "reason": f"{name} 在该智能体的高风险工具列表中",
            }
        )

        # ★ 返回哨兵而不是 await。工具**不执行** —— 续跑时 server 把这条整条
        #   删掉，tool_use 于是悬空，SuspensionMiddleware 跳去 tools 把它跑掉。
        return ToolMessage(
            content=suspend_marker("approval", approval_id),
            tool_call_id=tool_call_id,
            name=name,
        )


def _run_id_of(request: ToolCallRequest) -> str:
    """从工具调用的上下文里取 run_id。

    ★ 取不到时回落成空串：approval_id 于是只由 tool_call_id 决定。那仍然是
      **可复现**的（这是不死循环的唯一要求），只是不同 run 之间可能撞 id。
      宁可如此也不抛异常 —— 拿不到 run_id 不该让一次工具调用炸掉整轮。
    """
    for holder in (getattr(request, "runtime", None), getattr(request, "state", None)):
        if holder is None:
            continue
        found = (
            holder.get("run_id")
            if isinstance(holder, dict)
            else getattr(holder, "run_id", None)
        )
        if found:
            return str(found)
    return ""
