"""CLI 进程和 CLI 会话之间的两个接口（c4-model/agent/bridge/code-bridge-cli.html）。

    CliPort     一个可用的 CLI：拉起并握手、重启、终止。生产实现是 CliSupervisor
    CliEvents   CLI 的输出交给谁：内容更新、权限请求、意外退出。由 CLI 会话实现

★ CliEvents 只此一个定义：原先进程那一层和会话那一层各写了一份结构相同的回调接口。
"""

from __future__ import annotations

from typing import Any, Protocol

from atlas_host import FailCause

from .client import AcpClient, Negotiated

__all__ = ["CliEvents", "CliPort"]


class CliEvents(Protocol):
    async def on_update(self, raw: dict[str, Any]) -> None:
        """CLI 的一条 session/update，原始 JSON。"""
        ...

    async def on_permission_request(self, raw: dict[str, Any]) -> dict[str, Any]:
        """CLI 的权限请求。必须返回结果：CLI 的请求永远有回应。"""
        ...

    def on_cli_lost(self, cause: FailCause) -> None:
        """CLI 意外退出（主动重启、终止和预热期间的退出不报），在它的输出全部读完之后报一次。"""
        ...


class CliPort(Protocol):
    def bind(self, events: CliEvents) -> None: ...

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
