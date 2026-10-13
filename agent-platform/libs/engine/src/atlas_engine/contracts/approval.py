"""ApprovalGate —— 高风险工具人工确认的契约。

第四个能力协议。kernel 的 ApprovalMiddleware 在工具执行前问它「这次批了吗」；
server 注入 DB + Redis 实现（RedisApprovalGate），测试注入假的。
engine 因此不知道 Postgres 与 Redis 的存在。

★ 从「阻塞等待」改成「查询状态」（doc/detail/suspension.html §06）。

  原先是 `request()` 一路 await 到有人点头或 600s 超时 —— 一个 asyncio.Task
  全程挂着，进程重启即丢，而 600s 对小时级任务根本不够：一个跑 40 分钟的
  CLI 在第 35 分钟弹审批，用户没盯着屏幕就按超时拒绝处理了。

  现在 `check()` 立刻返回。还没结果时中间件产出一个挂起哨兵让本段结束
  （contracts/suspension.py），决策到达后续跑 —— 等待因此不占进程，可以等到
  第二天上班。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

__all__ = ["ApprovalGate", "ApprovalState", "Decision"]

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
        self, *, approval_id: str, tool_name: str, args: dict[str, Any]
    ) -> ApprovalState:
        """这次调用批了吗？**立刻返回，不阻塞**。

        实现方的职责：
          · 首次见到这个 approval_id 时登记它（落库），返回 "pending"
          · 之后返回它的真实状态

        ★ approval_id 必须由调用方**可复现地**派生，否则续跑时算出一个新 id、
          查到 pending、再挂起一次 —— 死循环。kernel 用
          `uuid5(NS, "{run_id}:{tool_call_id}")`，两者在一个 run 内唯一且跨段
          稳定（ApprovalMiddleware）。

        ★ expired 由实现方判定（超时即拒）。挂起之后没有进程在等，所以判定
          发生在恢复扫描里，而不是某个 await 的超时分支上。
        """
        ...
