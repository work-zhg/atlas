"""SandboxChannels：沙箱里某个端口的 WebSocket 通道，和「记一次活动」。

c4-model/agent/sandbox/code-sandbox-base-execution.html（浏览器的画面通道，
c4-model/agent/browser/code-browser-view.html）。

    open_ws(sandbox_uuid, port, …)   按 sandbox_uuid 找到沙箱里这个端口的集群内地址，连上 WebSocket
    keep_alive(sandbox_uuid)         记一次活动：推迟闲置回收（连着画面的人在慢慢登录）

★ 和执行命令、读写文件一样只收 sandbox_uuid：沙箱的地址只在基座里。端口上的服务怎么鉴权由调用方
  带请求头（如画面服务的口令），基座不认识。
★ 不走代理：对端永远是集群里的 Pod（macOS 的系统代理设置会把内网地址也拐进 SOCKS）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect

from ..lifecycle.server import Endpoint

if TYPE_CHECKING:
    from .ledger import ActivityLedger

__all__ = ["ChannelUnavailable", "EndpointProvider", "SandboxChannels"]


class ChannelUnavailable(ConnectionError):
    """连不上沙箱里的这个端口（服务没起来、拒绝了握手）。沙箱已经不在抛的是 SandboxGone。"""


class EndpointProvider(Protocol):
    """生命周期领域的 endpoint：这里只依赖这一个基座内部方法。"""

    async def endpoint(self, sandbox_uuid: UUID, port: int) -> Endpoint: ...


class SandboxChannels:
    def __init__(
        self, runtime: EndpointProvider, ledger: ActivityLedger, *, connect_timeout_s: float = 10.0
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._timeout = connect_timeout_s

    @asynccontextmanager
    async def open_ws(
        self,
        sandbox_uuid: UUID,
        port: int,
        *,
        path: str = "/",
        headers: Mapping[str, str] | None = None,
        subprotocols: Sequence[str] = (),
    ) -> AsyncIterator[ClientConnection]:
        """连上沙箱里 port 端口的 WebSocket，退出时关掉。沙箱不在抛 SandboxGone，连不上抛
        ChannelUnavailable。"""
        ep = await self._runtime.endpoint(sandbox_uuid, port)
        url = "ws://" + ep.url.removeprefix("http://").rstrip("/") + path
        try:
            ws = await connect(
                url,
                additional_headers={**ep.headers, **(headers or {})},
                subprotocols=list(subprotocols) or None,  # type: ignore[arg-type]
                compression=None,
                max_size=None,
                open_timeout=self._timeout,
                proxy=None,
            )
        except Exception as exc:
            raise ChannelUnavailable(f"连不上沙箱 {sandbox_uuid} 的端口 {port}：{exc}") from exc
        try:
            yield ws
        finally:
            await ws.close()

    async def keep_alive(self, sandbox_uuid: UUID) -> None:
        """记一次活动。只推迟闲置回收，不是占着沙箱：不再记了，沙箱照常按闲置时限回收。"""
        await self._ledger.touch(sandbox_uuid)
