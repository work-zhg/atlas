"""人工确认的共用部分：approval_id 的派生、给模型的几段结果文案、取 run_id。

判定「问不问人」的中间件是 permission.py 的 PermissionMiddleware（按审批模式 × 工具的副作用类别）。
原先这里的 ApprovalMiddleware（按作者配置的必审工具名单拦截）已被它取代，名单本身也已删除
（doc/native-approval-mode-design.html D7）。

★ 等待方式是「挂起」（doc/detail/suspension.html §06）：问一次 `gate.check(...)`，还没结果就在
  state.waiting_on 里登记一项让本段结束 —— 等待交给数据库，决策到达后续跑。挂起时**不执行、也不写
  任何 tool 消息**，tool_call 留着悬空（contracts/suspension.py）。
"""

from __future__ import annotations

import uuid
from typing import Any

__all__ = [
    "EXPIRED_RESULT",
    "GATE_FAILED_RESULT",
    "REJECTED_RESULT",
    "approval_id_for",
    "run_id_of",
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


def run_id_of(request: Any) -> str:
    """从工具调用的上下文里取 run_id —— runner 每段都把它放进图的 state（SuspensionState.run_id）。

    ★ 2026-10-22 之前 runner 没有传，这里永远回落成空串：approval_id 只由 tool_call_id 决定，
      不同 run 里同名的 tool_call_id 撞成同一条审批（测试里的假模型固定用 c1，连跑时互相污染）。
      续跑用的是 waiting_on 里存下的 token，不重新派生，所以修复前挂起的 run 不受影响。
    ★ 取不到时仍回落成空串（直接调中间件的单测没有 state）：那仍然是**可复现**的，
      不该让一次工具调用炸掉整轮。
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
