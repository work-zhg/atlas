"""UpstreamGateway：上游接入的唯一入口，进程内一份。

    discover   服务目录登记前预览、登记、重新导入、刷新时：发现能转换成哪些工具
    call       工具调用（数据面、试调）：按工具的映射调上游，结果统一成 MCP 的 CallToolResult

先按服务类型选上游（扩展点 Upstream），把上游凭据变成请求头、查询参数或 gRPC metadata，再交给它。
HTTP 连接、OAuth2 Token、gRPC 连接和描述池在这里复用。

★ 租户填的上游地址不能指向平台内部：每次连接前按解析出来的 IP 检查（guard.py）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ..settings import GatewaySettings
from . import DiscoveryFailed, UpstreamUnavailable
from .base import Upstream
from .credentials import TokenCache
from .grpc import GrpcConverter
from .guard import AddressGuard
from .mcp import McpProxy
from .rest import RestConverter
from .spec import Discovered, ToolBinding, UpstreamAuth, UpstreamSpec

__all__ = ["UpstreamGateway"]


class UpstreamGateway:
    def __init__(self, settings: GatewaySettings) -> None:
        # ★ 不设读超时：工具调用可能要很久（超时属于流量治理，单独设计）
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s, read=None, write=60.0, pool=30.0
            ),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
            trust_env=False,
            follow_redirects=False,
        )
        self.tokens = TokenCache(self.http)
        self.guard = AddressGuard(settings.upstream_allow_cidrs)
        self._grpc = GrpcConverter(discovery_timeout_s=settings.discovery_timeout_s)
        self._upstreams: dict[str, Upstream] = {
            "mcp": McpProxy(discovery_timeout_s=settings.discovery_timeout_s),
            "rest": RestConverter(self.http),
            "grpc": self._grpc,
        }

    async def aclose(self) -> None:
        await self.http.aclose()
        await self._grpc.aclose()

    async def check_addresses(self, spec: UpstreamSpec) -> None:
        """登记、改连接、改凭据时：地址不能指向平台内部。不行抛 UpstreamBlocked。"""
        await self.guard.check_all(spec.targets())

    async def discover(self, spec: UpstreamSpec) -> Discovered:
        """→ Discovered。拉不到、解析不了抛 DiscoveryFailed（原因给管理员看）。"""
        try:
            if spec.kind != "grpc" or spec.descriptor is None:  # 上传了描述文件的 gRPC 不用连
                await self.guard.check_all(spec.targets())
            return await self._upstream(spec).discover(spec, await self._auth(spec))
        except UpstreamUnavailable as exc:  # 地址不许连、换 OAuth2 Token 失败这类
            raise DiscoveryFailed(str(exc)) from exc

    async def call(
        self,
        spec: UpstreamSpec,
        binding: ToolBinding,
        arguments: Mapping[str, Any],
        identity: Mapping[str, str],
    ) -> dict[str, Any]:
        """identity：工具调用算好的身份请求头（服务关了「传递调用者身份」就是空的）。

        抛 UpstreamUnavailable（连不上）、UpstreamError（MCP 上游回了 JSON-RPC 错误）、
        InvalidArguments（参数转不成请求）。
        """
        await self.guard.check_all(spec.targets())
        auth = await self._auth(spec)
        return await self._upstream(spec).call(spec, binding, arguments, auth, identity)

    async def probe(self, url: str) -> int:
        """「测试连接」：GET 一下地址，→ HTTP 状态码。连不上抛 UpstreamUnavailable。"""
        await self.guard.check(url)
        try:
            r = await self.http.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return r.status_code

    def _upstream(self, spec: UpstreamSpec) -> Upstream:
        return self._upstreams[spec.kind]

    async def _auth(self, spec: UpstreamSpec) -> UpstreamAuth:
        cred = spec.credential
        try:
            headers, query = await cred.http(self.tokens)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"取上游凭据失败：{exc}") from exc
        return UpstreamAuth(headers, query, tuple(cred.grpc_metadata()))
