"""连上游：唯一连供应商的地方（UpstreamClient），连之前查地址并绑定校验后的 IP（AddressGuard）。

★ 上游地址是租户自己填的，网关替它去连。平台内部服务之间凭请求头上的身份互相信任，租户能让网关
  去连内部地址，就等于借网关冒充别人。
★ 防 DNS 重绑定（TOCTOU）：直连时在自定义 httpcore 网络后端的 connect_tcp 里一次性解析域名、
  逐个校验全部 IP，并直接连向校验通过的 IP 字面量，同时保留原请求 URL 的 Host 头与 TLS SNI 主机名，
  彻底消除「先查后连两次解析」的时间窗；同时显式禁掉重定向（follow_redirects=False）。
★ 出网代理（proxy）：海外域名在内网集群往往没有公网 DNS，不应在网关本机做外网域名解析；改为本地做
  静态 IP 与集群内网域名拦截（localhost / 无点短名 / .svc / .cluster.local / .internal / .local），
  由出网代理自己解析外网域名。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from .kernel import Egress, FailureKind, UpstreamFailure

__all__ = ["AddressGuard", "UpstreamClient", "UpstreamError"]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".svc", ".cluster.local")
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UpstreamError(Exception):
    """一次尝试在拿到回答之前就失败了（连不上、超时、上游回了错误状态码）。"""

    def __init__(self, failure: UpstreamFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _overloaded(message: str, *, timeout: bool = False) -> UpstreamError:
    return UpstreamError(UpstreamFailure(FailureKind.OVERLOADED, message, timeout=timeout))


def _is_internal_hostname(host: str) -> bool:
    clean = host.strip().lower().rstrip(".")
    if not clean:
        return True
    if clean == "localhost" or "." not in clean:
        return True
    return clean.endswith(_INTERNAL_SUFFIXES)


class AddressGuard:
    def __init__(self, allow_cidrs: Iterable[str] = (), *, enabled: bool = True) -> None:
        self._allow = [ipaddress.ip_network(c, strict=False) for c in allow_cidrs]
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

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

    async def resolve_and_check(self, host: str, port: int = 443) -> list[IPAddress]:
        """解析主机名并校验每一个 IP，返回校验通过的 IP 列表（供 TCP 连接直接绑定）。"""
        clean = host.strip().rstrip(".")
        if not clean:
            raise _overloaded("上游地址里没有主机名")
        try:
            ips: list[IPAddress] = [ipaddress.ip_address(clean.split("%", 1)[0])]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    clean, port, type=socket.SOCK_STREAM
                )
            except OSError as exc:
                raise _overloaded(f"解析不了上游地址 {clean}：{exc}") from exc
            seen: dict[IPAddress, None] = {}
            for info in infos:
                seen[ipaddress.ip_address(info[4][0].split("%", 1)[0])] = None
            ips = list(seen)
        if not ips:
            raise _overloaded(f"解析不了上游地址 {clean}")
        if self._enabled:
            bad = [str(ip) for ip in ips if self._denied(ip)]
            if bad:
                raise _overloaded(f"上游地址 {clean} 指向内部网络（{', '.join(bad)}），不连")
        return ips

    async def check(self, url: str) -> None:
        """解析出来的每个地址都不能是内部地址；解析不了按连不上处理。"""
        if not self._enabled:
            return
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            raise _overloaded(f"上游地址 {url!r} 里没有主机名")
        await self.resolve_and_check(host, parts.port or (443 if parts.scheme == "https" else 80))

    def check_proxy(self, url: str) -> None:
        """经出网代理连接时的本地检查：不解析外网域名，只拦内部 IP 与集群内网主机名。"""
        if not self._enabled:
            return
        reason = self.validate_static(url)
        if reason:
            raise _overloaded(f"上游地址 {urlsplit(url).hostname or url!r}：{reason}，不连")

    def validate_static(self, url: str) -> str | None:
        """登记时及代理模式的静态检查（不解析域名）：内部 IP 或集群内网域名直接拒绝。"""
        host = urlsplit(url).hostname or ""
        if not host:
            return "地址里没有主机名"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if self._enabled and not self._allow and _is_internal_hostname(host):
                return f"地址指向内部网络（{host}）"
            return None
        return f"地址指向内部网络（{ip}）" if self._enabled and self._denied(ip) else None


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """在 TCP 建连那一刻把域名解析结果校验并绑定成 IP 字面量，杜绝二次解析的 DNS 重绑定。"""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, guard: AddressGuard) -> None:
        self._inner = inner
        self._guard = guard

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        ips = await self._guard.resolve_and_check(host, port)
        last_exc: Exception | None = None
        for ip in ips:
            try:
                return await self._inner.connect_tcp(
                    str(ip),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._guard.enabled:
            raise _overloaded("不允许连接本地 Unix 套接字")
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class UpstreamClient:
    """连接池按出网方式分两份：直连（IP 绑定防重绑定）、经出网代理（静态防内网穿透）。"""

    def __init__(
        self,
        *,
        guard: Any,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self.guard = guard
        self._custom_transport = transport is not None
        timeout = httpx.Timeout(connect=connect_timeout_s, read=None, write=60.0, pool=30.0)
        limits = httpx.Limits(max_connections=1000, max_keepalive_connections=200)
        if transport is not None:
            self._direct = httpx.AsyncClient(
                timeout=timeout, limits=limits, transport=transport, follow_redirects=False
            )
            self._proxy: httpx.AsyncClient | None = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                proxy=proxy,
                transport=transport,
                follow_redirects=False,
            )
        else:
            direct_transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
            if isinstance(guard, AddressGuard):
                direct_transport._pool._network_backend = _GuardedBackend(
                    direct_transport._pool._network_backend, guard
                )
            self._direct = httpx.AsyncClient(
                timeout=timeout,
                transport=direct_transport,
                trust_env=False,
                follow_redirects=False,
            )
            self._proxy = (
                httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits,
                    proxy=proxy,
                    trust_env=False,
                    follow_redirects=False,
                )
                if proxy
                else None
            )

    def client(self, egress: Egress) -> httpx.AsyncClient:
        if egress is Egress.PROXY and self._proxy is not None:
            return self._proxy
        return self._direct

    async def _precheck(self, url: str, egress: Egress) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise _overloaded(f"上游地址 {url!r} 不合法")
        use_proxy = (
            egress is Egress.PROXY and self._proxy is not None and not self._custom_transport
        )
        if use_proxy:
            if hasattr(self.guard, "check_proxy"):
                self.guard.check_proxy(url)
            else:
                reason = self.guard.validate_static(url)
                if reason:
                    raise _overloaded(f"上游地址 {parts.hostname}：{reason}，不连")
            return
        if self._custom_transport or not isinstance(self.guard, AddressGuard):
            await self.guard.check(url)

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
        await self._precheck(url, egress)
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
        except UpstreamError:
            raise
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
