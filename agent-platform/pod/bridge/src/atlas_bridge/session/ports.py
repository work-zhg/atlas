"""session 层与相邻两层之间的接口（代码设计 §5 · §7.1）。

session 层不碰 socket、不碰进程：它只通过这里的 Protocol 与 Outbox（上）和 agent（下）交互，
因此可以在没有进程、没有网络、没有真实时间的情况下完整测试。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from atlas_host import FailCause, Model

from ..agent.client import AcpClient, Negotiated

__all__ = ["AgentEvents", "AgentPort", "MessageKind", "OutboxSink"]

#: control = 状态类消息，永不淘汰；data = agent.update，缓冲溢出时可淘汰（Bridge 设计 §7.4）
MessageKind = Literal["control", "data"]


class OutboxSink(Protocol):
    """session 层产生的消息一律放进这里。

    ★ 同步方法：状态转换与放入消息在同一个同步段里完成，seq 的顺序即转换的顺序（代码设计 §6.2）。
    ★ seq 由 Outbox 分配，所以传入的是「给定 seq 构造参数」的函数。
    """

    def put_notification(
        self, method: str, build: Callable[[int], Model], *, kind: MessageKind
    ) -> int: ...

    def put_request(self, method: str, build: Callable[[int], Model], *, request_id: int) -> int:
        """bridge → server 的请求（permission.ask）。id 由调用方分配，补发时沿用（§7.3）。"""
        ...

    async def wait_writable(self) -> None:
        """背压：缓冲满且连接在时等待（§6.6）。数据面消息放入之前调用。"""
        ...

    def resend_plan(self, last_seq: int) -> tuple[int, list[tuple[int, int]]]:
        """session.attach：server 已处理到 last_seq → (补发起点, 无法补发的区间)。"""
        ...


class AgentEvents(Protocol):
    """agent 的输出回到 session 层的入口。由 HostSession 实现。"""

    async def on_agent_update(self, raw: dict[str, Any]) -> None: ...

    async def on_permission_request(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    def on_agent_lost(self, cause: FailCause) -> None: ...


class AgentPort(Protocol):
    """一个可用的 agent：拉起、握手、终止。生产实现是 AgentSupervisor。"""

    def bind(self, events: AgentEvents) -> None: ...

    async def boot(self) -> Negotiated: ...

    async def restart(self, cause: str) -> Negotiated:
        """终止 → 按节制等待 → 重新启动。超过节制抛 AgentUnavailable（§5.6）。"""
        ...

    async def shutdown(self) -> None: ...

    @property
    def restarts(self) -> int:
        """会话期间累计的重启次数。"""
        ...

    @property
    def client(self) -> AcpClient: ...

    @property
    def negotiated(self) -> Negotiated: ...
