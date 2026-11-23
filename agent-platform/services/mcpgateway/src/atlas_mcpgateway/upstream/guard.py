"""上游地址的检查（多租户设计 §8「租户填的地址不能指向平台内部」）。

上游地址（MCP 地址、REST 基地址和 OpenAPI 地址、gRPC 地址、OAuth2 的 Token 地址）是租户自己填的，
网关替它去连。平台内部服务之间凭请求头上写的身份互相信任，租户能让网关去连内部地址，就等于借网关
冒充别人。所以：

    登记、改连接时    检查一次（地址不对直接 422，给管理员看原因）
    每次真正连接前    按解析出来的 IP 再检查一次：防止域名先指向外网、事后改成指向内网

不允许的：回环、链路本地（含云厂商的元数据地址 169.254.169.254）、私有网段（含集群的 Pod / Service
网段）、运营商级 NAT、组播、保留、未指定地址。私有化部署里上游确实在内网的，部署时把网段加进
settings.upstream_allow_cidrs。

★ 只防「借网关进内网」，不做别的流量治理。检查和真正连接之间仍有极短的窗口（DNS 改得足够快时），
  这是按 IP 连接之外能做到的程度。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from . import UpstreamUnavailable

__all__ = ["AddressGuard", "UpstreamBlocked"]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UpstreamBlocked(UpstreamUnavailable):
    """上游地址指向内部网络：没有连，也不会连。"""


def _host_port(target: str) -> tuple[str, int | None]:
    """URL（http://h:p/…）或 gRPC 地址（h:p、[v6]:p、dns:///h:p）→ (主机, 端口)。"""
    raw = target.strip()
    if "://" not in raw or raw.startswith("dns:"):
        raw = "//" + raw.removeprefix("dns:").lstrip("/")
    parts = urlsplit(raw)
    return (parts.hostname or "", parts.port)


class AddressGuard:
    def __init__(self, allow_cidrs: Iterable[str] = ()) -> None:
        self._allow = [ipaddress.ip_network(c, strict=False) for c in allow_cidrs]

    def _denied(self, ip: IPAddress) -> bool:
        if any(ip in net for net in self._allow):
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return self._denied(mapped)
        return (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or (ip.version == 4 and ip in _CGNAT)
        )

    async def check(self, target: str) -> None:
        """解析出来的每个地址都不能是内部地址。解析不了按连不上处理。"""
        host, port = _host_port(target)
        if not host:
            raise UpstreamBlocked(f"上游地址 {target!r} 里没有主机名")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            ips: list[IPAddress] = [literal]
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise UpstreamUnavailable(f"解析不了上游主机 {host}：{exc}") from exc
            ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
        bad = sorted({str(ip) for ip in ips if self._denied(ip)})
        if bad:
            raise UpstreamBlocked(f"上游地址 {host} 指向内部网络（{', '.join(bad)}），不能连接")

    async def check_all(self, targets: Iterable[str]) -> None:
        for target in targets:
            await self.check(target)
