"""挂起：「这一段到此为止，等一个外部事件，事件到了续跑」。

★ 核心判断：**SubAgent 挂起与审批挂起是同一件事**（doc/detail/suspension.html §02）。

    SubAgent  子智能体要跑一小时 —— 父 run 不该在进程里等那么久
    审批      要等人点头 —— 可能等到第二天上班

  两者都是「等外部」，差别只有一处：审批挂起时那个工具**还没执行**，续跑时
  要先把它跑掉；SubAgent 挂起时子 run 已经起了，续跑时把结论填进去即可。
  这条差异由各自的中间件处理，SuspensionMiddleware 只认识「有东西在等」。

★ 「在等什么」只记在 state 的 `waiting_on` 里，消息里没有任何占位
  （doc/detail/suspension.html 修正 13；demo/suspend-resume）：

    waiting_on   tool_call_id → Wait。能力方（审批 / SubAgent 中间件）拦下调用时
                 登记，有了结果时删掉。挂起时 server 从图的最终 state 里取出来、
                 与消息**在同一个事务里**落进 run.waiting_on；续跑时原样交回
    decisions    token → 结果。续跑时 server 交进来，只进不出：
                   approval    "approved" / "rejected" / "expired" / "failed"
                   subagent    子 run 的结论文本
                 ★ 只放**已经有结果**的。放了 pending 的话 before_model 放行、
                   审批中间件又登记一次，两边来回跳到 GraphRecursionError

  被拦下的 tool_call 因此在消息里**悬空**。配对规则（每个 tool_use 恰好一条
  tool_result）只约束交给模型的那一刻：挂起时图在 before_model 就跳出、不调
  模型；续跑时先执行工具再调模型。悬空有两种含义，靠 waiting_on 区分 ——
  在里面的是「在等」，不在的是「被放弃」（补 cancelled / 中断）。

★ 本模块是 contracts 层，不依赖 langchain：state schema 本身（带 reducer 与
  可见性标注）在 kernel/middleware/suspension.py::SuspensionState。
"""

from __future__ import annotations

from typing import Literal, TypedDict

__all__ = [
    "SuspensionReason",
    "Wait",
    "WaitingOn",
    "merge_waits",
]

SuspensionReason = Literal["subagent", "approval"]


class Wait(TypedDict):
    """一个被拦下的 tool_call 在等什么。

    ★ reason 必须带上。续跑的 barrier 按它分派：SubAgent 等「子 run 终态」，审批等
      「approval 被决策」。只记 token 的话审批挂起会被当成 SubAgent —— 审批挂起时
      没有子 run，barrier 立刻满足，于是续跑、又挂起、再续跑，**死循环**。
    """

    reason: SuspensionReason
    #: subagent → 子 run 的 id；approval → approval 的 id。也是 decisions 的 key。
    token: str


#: tool_call_id → Wait。state 与 run.waiting_on 是同一个形状。
WaitingOn = dict[str, Wait]


def merge_waits(old: WaitingOn | None, new: dict[str, Wait | None] | None) -> WaitingOn:
    """waiting_on 的 reducer：按 tool_call_id 合并，值为 None 表示删除。

    ★ 不能用 operator.add：拿到结果后要把那一项**删掉**，add 只能加。
    ★ 必须有 reducer：一批里几个调用同时被拦下时，它们在同一步写 waiting_on，
      没有 reducer 会报 InvalidUpdateError；删除写的 None 也会原样覆盖进去。
    """
    merged = {**(old or {}), **(new or {})}
    return {k: v for k, v in merged.items() if v is not None}
