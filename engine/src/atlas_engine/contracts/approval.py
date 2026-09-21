"""ApprovalGate —— 高风险工具人工确认的等待契约。

第四个能力协议。kernel 的 ApprovalMiddleware 在工具执行前调用它阻塞等待；
server 注入 DB + Redis 实现（RedisApprovalGate），测试注入假的。
engine 因此不知道 Postgres 与 Redis 的存在。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

__all__ = ["ApprovalGate", "Decision"]

Decision = Literal["approved", "rejected", "expired"]


class ApprovalGate(Protocol):
    """等待人工决策。expired 由实现方判定（超时即拒）。"""

    async def request(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any]
    ) -> Decision: ...
