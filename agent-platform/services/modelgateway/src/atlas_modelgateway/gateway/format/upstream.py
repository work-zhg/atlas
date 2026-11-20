"""连上游：唯一连供应商的地方（UpstreamClient），连之前查地址（AddressGuard）。

★ 上游地址是租户自己填的，网关替它去连。平台内部服务之间凭请求头上的身份互相信任，租户能让网关
  去连内部地址，就等于借网关冒充别人。所以每次真正连接前，按解析出来的 IP 再查一次（防止域名先
  指向外网、事后改成指向内网）。私有化部署里上游确实在内网的（自部署 vLLM），把网段加进
  upstream_allow_cidrs。和 MCP 网关同一套规则（各自一份，服务之间不共享代码包）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..kernel import Egress, FailureKind, UpstreamFailure

__all__ = ["AddressGuard", "UpstreamClient", "UpstreamError"]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UpstreamError(Exception):
    """一次尝试在拿到回答之前就失败了（连不上、超时、上游回了错误状态码）。"""

    def __init__(self, failure: UpstreamFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _overloaded(message: str, *, timeout: bool = False) -> UpstreamError:
    return UpstreamError(UpstreamFailure(FailureKind.OVERLOADED, message, timeout=timeout))


class AddressGuard:
    def __init__(self, allow_cidrs: Iterable[str] = (), *, enabled: bool = True) -> None:
        self._allow = [ipaddress.ip_network(c, strict=False) for c in allow_cidrs]
        self._enabled = enabled

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

    async def check(self, url: str) -> None:
        """解析出来的每个地址都不能是内部地址；解析不了按连不上处理。"""
        if not self._enabled:
            return
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            raise _overloaded(f"上游地址 {url!r} 里没有主机名")
        try:
            ips: list[IPAddress] = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, parts.port or 443, type=socket.SOCK_STREAM
                )
            except OSError as exc:
                raise _overloaded(f"解析不了上游地址 {host}：{exc}") from exc
            ips = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
        bad = [str(ip) for ip in ips if self._denied(ip)]
        if bad:
            raise _overloaded(f"上游地址 {host} 指向内部网络（{', '.join(bad)}），不连")

    def validate_static(self, url: str) -> str | None:
        """登记时的检查（不解析域名）：写死了内部 IP 的直接拒绝。返回原因，没问题返回 None。"""
        host = urlsplit(url).hostname or ""
        if not host:
            return "地址里没有主机名"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        return f"地址指向内部网络（{ip}）" if self._enabled and self._denied(ip) else None


class UpstreamClient:
    """连接池按出网方式分两份：直连、经出网代理。"""

    def __init__(
        self,
        *,
        guard: AddressGuard,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self.guard = guard
        timeout = httpx.Timeout(connect=connect_timeout_s, read=None, write=60.0, pool=30.0)
        limits = httpx.Limits(max_connections=1000, max_keepalive_connections=200)
        self._direct = httpx.AsyncClient(timeout=timeout, limits=limits, transport=transport)
        self._proxy = (
            httpx.AsyncClient(timeout=timeout, limits=limits, proxy=proxy, transport=transport)
            if proxy or transport
            else None
        )

    def client(self, egress: Egress) -> httpx.AsyncClient:
        if egress is Egress.PROXY and self._proxy is not None:
            return self._proxy
        return self._direct

    async def send(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Any,
        egress: Egress,
        stream: bool,
        read_timeout_s: float | None,
        method: str = "POST",
    ) -> httpx.Response:
        """发出请求，拿到响应头就返回（流式时回答体还没读）。连不上、超时抛 UpstreamError。"""
        await self.guard.check(url)
        client = self.client(egress)
        request = client.build_request(
            method,
            url,
            headers=dict(headers),
            json=body if method == "POST" else None,
            timeout=httpx.Timeout(
                connect=client.timeout.connect,
                read=None if stream else read_timeout_s,
                write=60.0,
                pool=30.0,
            ),
        )
        try:
            if stream:
                return await asyncio.wait_for(client.send(request, stream=True), read_timeout_s)
            return await client.send(request)
        except TimeoutError as exc:
            raise _overloaded("等不到上游的回答（超时）", timeout=True) from exc
        except httpx.TimeoutException as exc:
            raise _overloaded(f"等不到上游的回答（{type(exc).__name__}）", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise _overloaded(f"连不上上游：{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        await self._direct.aclose()
        if self._proxy is not None:
            await self._proxy.aclose()
