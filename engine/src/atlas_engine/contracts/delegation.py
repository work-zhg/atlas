"""DelegationProtocol —— 委派的受理契约。

与 Filesystem / Sandbox 并列的第三个能力协议。kernel 的 `task` 工具在委派
模式下调用它；server 的 SubagentService 是唯一实现（在子智能体自己的会话
上起一个子 run）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["DelegationProtocol", "delegation_pending"]


def delegation_pending(sub_run_id: str) -> str:
    """「这次委派还没有结果，本段到此为止」—— 返回一个挂起哨兵。

    ★ 委派只是挂起的**一个** reason，审批是另一个。共用的机制在
      `contracts/suspension.py`：哨兵格式、解析、以及图内那个唯一的跳出点。
      这个函数存在只为让受理方不必自己拼 reason 字面量。
    """
    from atlas_engine.contracts.suspension import suspend_marker

    return suspend_marker("delegation", sub_run_id)


@runtime_checkable
class DelegationProtocol(Protocol):
    """委派的受理方 —— `task` 工具在委派模式下唯一认识的对象。

    与 `FilesystemProtocol` / `SandboxProtocol` 并列的第三个能力协议。
    Atlas 注入它之后，`task` 不再在图内跑子图，而是把任务交给受理方，
    由它在子智能体**自己的会话**上起一个子 run。kernel 只保留工具形态与
    结果整形；会话解析、技能投送、准入控制都在实现方（server）那侧。

    ★ 刻意是**一个对象管全部子智能体**，不是每个子智能体一个对象：
      准入（全局并发 / 单 run 累计）、同轮去重、并行约束都是跨智能体的
      关切，拆成 per-agent 对象就没有地方放它们了。名录的权威也不在这里
      —— 有哪些子智能体由 spec（版本快照）回答，本协议只负责执行。
    """

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        """把任务书交给名为 `name` 的子智能体，返回它的最终文本。

        实现抛出的异常由工具捕获成错误文本回给模型 —— 一次委派失败
        （撞上限、同轮重名）是可恢复的局部问题，不该炸掉整个 run。
        """
        ...
