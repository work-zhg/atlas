"""SubAgentSpawner —— 起 SubAgent 的契约。

与 Filesystem / Sandbox / ApprovalGate 并列的能力协议。kernel 的
SubAgentMiddleware 在 `task` 调用时问它要一个子 run；server 的 SubAgentService
是唯一实现（在 SubAgent 自己的会话上起一个子 run）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .permission import ApprovalMode

__all__ = ["SubAgentSpawner"]


@runtime_checkable
class SubAgentSpawner(Protocol):
    """起一个 SubAgent 的子 run —— `task` 唯一认识的对象。

    ★ 只负责「起」，**立刻返回，不等它跑完**（与 ApprovalGate.check 同一个姿态）。
      调用方随即在 state.waiting_on 里登记这个子 run 并挂起本段；子 run 跑完叫醒
      父 run，续跑时它的结论作为 `task` 的结果交回（contracts/suspension.py）。
      一次 SubAgent 可以跑一小时 —— 父 run 不该在进程里等那么久。

    ★ 刻意是**一个对象管全部 SubAgent**，不是每个 SubAgent 一个对象：
      准入（全局并发 / 整棵树累计）、同轮同名、会话串行都是跨 SubAgent 的
      关切，拆成 per-agent 对象就没有地方放它们了。名录的权威也不在这里
      —— 有哪些 SubAgent 由 spec（版本快照）回答，本协议只负责起子 run。
    """

    async def spawn(
        self,
        name: str,
        task: str,
        *,
        fresh: bool = False,
        approval_mode: ApprovalMode | None = None,
    ) -> str:
        """把任务书交给名为 `name` 的 SubAgent，起一个子 run，返回它的 id。

        approval_mode 是派活那一刻父 run 的审批模式（图的 state 里的当前值，
        计划获批后可能已经和 run 开始时不同）。子 run 据此定自己的模式（设计 D6）。

        起不了（撞上限、同轮同名、会话被占）就抛异常，消息是**给模型看的**：
        调用方把它接成一条失败的工具结果 —— 一次派不出去是可恢复的局部问题，
        模型可以改派、合并任务或自己动手，不该炸掉整个 run。
        """
        ...
