"""ApprovalGate —— 高风险工具人工确认的契约。

第四个能力协议。kernel 的 PermissionMiddleware 在工具执行前问它「这次批了吗」；
server 注入 DB + Redis 实现（RedisApprovalGate），测试注入假的。
engine 因此不知道 Postgres 与 Redis 的存在。

★ 从「阻塞等待」改成「查询状态」（doc/detail/suspension.html §06）。

  原先是 `request()` 一路 await 到有人点头或 600s 超时 —— 一个 asyncio.Task
  全程挂着，进程重启即丢，而 600s 对小时级任务根本不够：一个跑 40 分钟的
  CLI 在第 35 分钟弹审批，用户没盯着屏幕就按超时拒绝处理了。

  现在 `check()` 立刻返回。还没结果时中间件在 state.waiting_on 里登记一项让本段结束
  （contracts/suspension.py），决策到达后续跑 —— 等待因此不占进程，可以等到
  第二天上班。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Literal, Protocol

__all__ = ["ApprovalGate", "ApprovalState", "Decision", "approval_id_for"]

Decision = Literal["approved", "rejected", "expired"]

#: `check()` 的返回值。比 Decision 多两个：
#:
#:   pending   还没有结果 —— 门禁登记好了，在等人
#:   failed    **门禁自己坏了** —— 没能登记，不是任何人的决定
#:
#: ★ 为什么 failed 要单独存在，而不是复用 rejected。
#:
#:   两者的**处理**相同（都不放行 —— 高风险工具无人把关地执行是最不能接受的
#:   失败方向），但**含义**相反：rejected 是系统正常工作的结果，failed 是系统
#:   没工作。压成一个值的代价在排查上：2026-09-23 一次 `tool_name` 超列宽导致
#:   落库异常，被当成「用户拒绝」原样喂给 CLI，CLI 停手、子 run 零产出，父
#:   模型看到的只有一句「没有产出文本结论」—— 界面上没有任何线索指向数据库，
#:   根因只能从 Pod 日志里翻出来。
#:
#:   调用方据此给出不同的文本：拒绝说「用户拒绝了」，故障说「审批系统故障」。
#:   两句话的排查成本差一个数量级。
ApprovalState = Literal["pending", "approved", "rejected", "expired", "failed"]


class ApprovalGate(Protocol):
    """查询一次审批的状态；首次查询时顺便把它登记下来。"""

    async def check(
        self,
        *,
        approval_id: str,
        tool_name: str,
        args: dict[str, Any],
        kind: str = "tool",
        source: str | None = None,
        reason: str | None = None,
        tool_call_id: str | None = None,
        execution_modes: Sequence[str] | None = None,
    ) -> ApprovalState:
        """这次调用批了吗？**立刻返回，不阻塞**。

        实现方的职责：
          · 首次见到这个 approval_id 时登记它（落库），返回 "pending"
          · 之后返回它的真实状态

        kind / source / reason 只在登记时落库（审计与界面用）：
          kind    tool = 工具调用的确认；plan = 规划模式提交的计划（contracts/permission.py）
          source  为什么问：manual_mode / plan_mode / auto_uncertain（没有配置判断器）/
                  judge_failed（判断器出错）
          reason  给用户看的一句话
          tool_call_id  被拦下的那个调用；一个调用至多一件待决的事（approval 表的唯一键）
          execution_modes  计划审批：批准时允许选的执行模式。登记时记下，做决定时按它校验 ——
                  模式是权限，作者之后收窄了配置，这次计划也按提交那一刻的范围

        ★ approval_id 必须由调用方**可复现地**派生，否则续跑时算出一个新 id、
          查到 pending、再挂起一次 —— 死循环。kernel 用
          `uuid5(NS, "{run_id}:{tool_call_id}")`，两者在一个 run 内唯一且跨段
          稳定（kernel/middleware/approval.py）。

        ★ expired 由实现方判定（超时即拒）。挂起之后没有进程在等，所以判定
          发生在恢复扫描里，而不是某个 await 的超时分支上。
        """
        ...


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
