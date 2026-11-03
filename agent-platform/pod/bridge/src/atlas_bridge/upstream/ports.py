"""上游通道和 CLI 会话之间的两个接口（c4-model/agent/bridge/code-bridge-upstream.html）。

    UpstreamOutbox    发往 server 的唯一出口：CLI 会话、一轮、权限询问都只依赖它
    SessionCommands   通道对会话的全部要求，由 CLI 会话实现；通道因此不认识会话的状态机

两边都只认这里的 Protocol，所以都能单独测试：会话不碰 socket，通道不碰状态机。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal, Protocol

from atlas_host import (
    Model,
    OpenParams,
    OpenResult,
    SessionState,
    TurnCancelResult,
    TurnRef,
    TurnStartParams,
    TurnStartResult,
)

__all__ = ["MessageKind", "SessionCommands", "UpstreamOutbox"]

#: control = 状态类消息，永不淘汰；data = agent.update，缓冲溢出时可淘汰（Bridge 设计 §7.4）
MessageKind = Literal["control", "data"]


class UpstreamOutbox(Protocol):
    """会话、一轮、权限询问产生的消息一律放进这里。

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


class SessionCommands(Protocol):
    """上游通道对 CLI 会话的全部要求。"""

    #: 会话结束（不论谁发起）时 set；通道据此以 4410 关闭连接、不再接受新连接
    ended: asyncio.Event

    def readiness(self) -> tuple[bool, str]:
        """/readyz：(就绪了吗 = CLI 已完成初始化, 给人看的阶段名)。"""
        ...

    async def open(self, params: OpenParams) -> OpenResult: ...

    async def start_turn(self, params: TurnStartParams) -> TurnStartResult: ...

    def cancel_turn(self, turn_id: str) -> TurnCancelResult: ...

    async def close(self) -> None: ...

    def snapshot(self, bridge_instance: str) -> tuple[SessionState, TurnRef | None]:
        """session.attach：会话此刻的状态和当前一轮。实例号对不上（bridge 重启过）或会话没打开
        抛 SessionNotOpen。补发计划由通道按 Outbox 算，不在这里。"""
        ...

    def answer_permission(self, ask_id: int, result: Any = None, error: Any = None) -> None:
        """server 对 permission.ask 的响应（按 JSON-RPC id 转交）。"""
        ...

    def upstream_changed(self, connected: bool) -> None:
        """上游连接建立 / 断开。断开不结束这一轮。"""
        ...
