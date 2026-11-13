"""CLI 跑在哪里（c4-model/agent/cli-runtime/code-cli-runtime.html · CliHost）。

CLI 运行时定义的扩展点：给它一个会话、CLI 配置和本轮的能力，拿回一个能连的 bridge 地址和
CLI 实际看到的能力。两个实现：

    PodHost         会话 Pod（sandbox/session_pod 的 SessionPods）：能力原样交给 Pod 里的 CLI
    DeviceSessions  使用者自己的电脑（device/sessions.py）：能力交给设备网关登记，CLI 看到的地址
                    都指向设备网关，凭证只有会话令牌

    HostRouter      按运行位置（CliSpec.placement）选一个

★ 连上之后的事（上游协议、序号确认、断线补发、审批）两种运行位置完全一样，CLI 运行时不区分。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from atlas_engine.contracts.spec import CliSpec

    from ..db.models import Thread
    from ..sandbox.session_pod import PodProvider
    from .capabilities import CliCapabilities

__all__ = ["CliHost", "HostAccess", "HostRouter", "HostUnavailable", "PodHost"]


class HostAccess(NamedTuple):
    """连上一个会话的 bridge 需要的全部信息，和 CLI 实际看到的能力。"""

    #: ws 地址：会话 Pod 里这个会话的 bridge，或设备网关上这个会话的地址
    url: str
    #: 服务令牌（会话 Pod 的 bridge 令牌，或设备网关的服务令牌）
    token: str
    #: CLI 看到的能力：本机 CLI 的 MCP 地址换成了设备网关上的、凭证换成了会话令牌
    caps: CliCapabilities


class HostUnavailable(RuntimeError):
    """运行位置此刻用不了，而且用户自己能处理（电脑不在线、没有默认设备……）。

    message 原样显示给用户；别的异常（集群、沙箱服务的故障）只说「系统忙」。
    """

    def __init__(self, message: str, *, kind: str = "host_unavailable") -> None:
        super().__init__(message)
        self.kind = kind


class CliHost(Protocol):
    async def ensure(self, thread: Thread, cli: CliSpec, caps: CliCapabilities) -> HostAccess:
        """确保这个会话的 bridge 在跑。每轮调一次（断线重连前也调），不在外面缓存。"""
        ...


class PodHost:
    """会话 Pod 作为 CliHost：能力原样交给 Pod 里的 CLI。"""

    def __init__(self, pods: PodProvider) -> None:
        self._pods = pods

    async def ensure(self, thread: Thread, cli: CliSpec, caps: CliCapabilities) -> HostAccess:
        endpoint = await self._pods.ensure(
            thread, cli, model=caps.model, credentials=caps.credentials
        )
        return HostAccess(endpoint.url, endpoint.token, caps)


class HostRouter:
    """按运行位置选 CliHost。没部署设备网关时，本机 CLI 明确报错。"""

    def __init__(self, pods: CliHost, devices: CliHost | None = None) -> None:
        self._pods = pods
        self._devices = devices

    async def ensure(self, thread: Thread, cli: CliSpec, caps: CliCapabilities) -> HostAccess:
        if not cli.on_device:
            return await self._pods.ensure(thread, cli, caps)
        if self._devices is None:
            raise HostUnavailable(
                "这个部署没有设备网关，本机 CLI 智能体用不了", kind="device_unavailable"
            )
        return await self._devices.ensure(thread, cli, caps)
