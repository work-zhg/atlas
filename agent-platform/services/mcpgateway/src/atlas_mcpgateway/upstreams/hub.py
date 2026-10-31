"""上游的运行时：按服务类型发现工具、调用工具。

进程内一份：HTTP 连接、OAuth2 Token、gRPC 连接共用。

discover   登记、重新导入、刷新时：MCP 拉 tools/list；REST 取并解析 OpenAPI；
           gRPC 服务端反射或解析上传的描述文件
call       数据面（和试调）：按工具的映射调上游，结果统一成 MCP 的 CallToolResult
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from ..db.models import McpService, McpTool
from ..definitions import Definition, normalize
from ..settings import GatewaySettings
from . import CallContext, DiscoveryFailed, UpstreamUnavailable
from . import grpc as grpc_up
from . import rest as rest_up
from .credentials import Credential, TokenCache
from .mcp import McpUpstream

__all__ = ["Discovered", "Upstreams"]

_tracer = trace.get_tracer("atlas_mcpgateway")


@dataclass
class Discovered:
    """发现的结果。

    mcp    definitions：上游的全部工具（全部登记，不用挑）
    rest   operations：OpenAPI 的操作（管理员挑）；没给 OpenAPI 地址时为空（手工添加）
    grpc   methods：描述里的方法（管理员挑）；descriptor：要存库的 FileDescriptorSet
    """

    kind: str
    definitions: list[Definition] = field(default_factory=list)
    operations: list[rest_up.Operation] = field(default_factory=list)
    methods: list[grpc_up.Method] = field(default_factory=list)
    descriptor: bytes | None = None
    services: list[str] = field(default_factory=list)


class Upstreams:
    def __init__(self, settings: GatewaySettings) -> None:
        self.settings = settings
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
        self.channels = grpc_up.Channels()
        self.pools = grpc_up.Pools()

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.channels.aclose()

    # ── 发现

    async def discover(
        self,
        kind: str,
        connection: Mapping[str, Any],
        credential: Credential,
        *,
        service: str,
        descriptor: bytes | None = None,
    ) -> Discovered:
        """→ Discovered。拉不到、解析不了抛 DiscoveryFailed（原因给管理员看）。"""
        try:
            return await self._discover(
                kind, connection, credential, service=service, descriptor=descriptor
            )
        except UpstreamUnavailable as exc:  # 换 OAuth2 Token 失败这类
            raise DiscoveryFailed(str(exc)) from exc

    async def _discover(
        self,
        kind: str,
        connection: Mapping[str, Any],
        credential: Credential,
        *,
        service: str,
        descriptor: bytes | None,
    ) -> Discovered:
        if kind == "mcp":
            headers, _ = await self._http_auth(credential)
            upstream = McpUpstream(
                connection["url"], transport=connection["transport"], headers=headers
            )
            try:
                raw = await upstream.list_tools(timeout_s=self.settings.discovery_timeout_s)
            except UpstreamUnavailable as exc:
                raise DiscoveryFailed(f"连不上 MCP Server：{exc}") from exc
            return Discovered(kind, definitions=[normalize(service, t) for t in raw])
        if kind == "rest":
            if not connection.get("spec_url"):
                return Discovered(kind)
            headers, query = await self._http_auth(credential)
            doc = await rest_up.fetch_spec(
                self.http, connection["spec_url"], headers=headers, query=query
            )
            return Discovered(kind, operations=rest_up.parse(doc))
        data = descriptor
        if data is None:
            data = await grpc_up.reflect(
                self._channel(connection, credential),
                metadata=credential.grpc_metadata(),
                timeout_s=self.settings.discovery_timeout_s,
            )
        return Discovered(
            kind,
            methods=grpc_up.methods(data),
            descriptor=data,
            services=grpc_up.services_in(data),
        )

    # ── 调用

    async def call(
        self,
        svc: McpService,
        tool: McpTool,
        arguments: Mapping[str, Any],
        ctx: CallContext,
    ) -> dict[str, Any]:
        """→ MCP 线上格式的 CallToolResult。

        抛 UpstreamUnavailable（连不上）、UpstreamError（MCP 上游回了 JSON-RPC 错误）、
        grpc.InvalidArguments（参数转不成请求消息）。
        """
        credential = Credential.of(svc.credential, svc.credential_secret)
        identity = ctx.identity()
        if svc.kind == "mcp":
            headers, _ = await self._http_auth(credential)
            upstream = McpUpstream(
                svc.connection["url"], transport=svc.connection["transport"], headers=headers
            )
            return await upstream.call_tool(tool.name, arguments, identity=identity)
        if svc.kind == "rest":
            mapping = tool.mapping or {}
            headers, query = await self._http_auth(credential)
            with _tracer.start_as_current_span(
                f"{mapping.get('method')} {mapping.get('path')}",
                kind=SpanKind.CLIENT,
                attributes={
                    "http.request.method": str(mapping.get("method")),
                    "url.template": str(mapping.get("path")),
                },
            ):
                return await rest_up.call(
                    self.http,
                    svc.connection["base_url"],
                    mapping,
                    arguments,
                    headers={**headers, **identity},
                    query=query,
                )
        mapping = tool.mapping or {}
        if svc.descriptor is None:
            raise UpstreamUnavailable("这个 gRPC 服务还没有接口描述，请重新发现方法")
        metadata = [*credential.grpc_metadata(), *((k.lower(), v) for k, v in identity.items())]
        with _tracer.start_as_current_span(
            str(mapping.get("method")),
            kind=SpanKind.CLIENT,
            attributes={"rpc.system": "grpc", "rpc.method": str(mapping.get("method"))},
        ):
            return await grpc_up.call(
                self._channel(svc.connection, credential),
                self.pools.get(svc.descriptor),
                mapping,
                arguments,
                metadata=metadata,
            )

    # ── 内部

    async def _http_auth(self, credential: Credential) -> tuple[dict[str, str], dict[str, str]]:
        try:
            return await credential.http(self.tokens)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"取上游凭据失败：{exc}") from exc

    def _channel(self, connection: Mapping[str, Any], credential: Credential) -> Any:
        tls = str(connection.get("tls") or "plaintext")
        return self.channels.get(
            str(connection["address"]),
            tls,
            cert=credential.get("cert") if credential.type == "mtls" else None,
            key=credential.secret if credential.type == "mtls" else None,
        )


def decode_descriptor(raw: str | None) -> bytes | None:
    """管理接口上传的描述文件（base64）→ bytes。"""
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise DiscoveryFailed("描述文件不是 base64 编码的 FileDescriptorSet") from exc
