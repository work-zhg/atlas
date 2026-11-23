"""上游接入的值对象：调用方（服务目录、工具调用）把要连的上游描述成这些交过来。

    UpstreamSpec   连一个上游要的全部东西：类型、连接、上游凭据、gRPC 描述
                   （McpService.upstream_spec）
    ToolBinding    怎么调一个工具：名字、映射（McpTool.binding）。MCP 代理只有名字
    UpstreamAuth   上游凭据变成的请求头、查询参数、gRPC metadata（UpstreamGateway 算好交给上游）
    Discovered     发现的结果

★ 上游接入因此不认识服务目录的表：表结构变了只改服务目录里的转换。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .credentials import UpstreamCredential

if TYPE_CHECKING:
    from .grpc import Method
    from .rest import Operation

__all__ = ["Discovered", "ToolBinding", "UpstreamAuth", "UpstreamSpec", "upstream_targets"]


def upstream_targets(
    kind: str, connection: Mapping[str, Any], credential: Mapping[str, Any] | None = None
) -> list[str]:
    """一个服务会去连的全部地址（地址检查用，guard.py）。"""
    keys = {"mcp": ("url",), "rest": ("base_url", "spec_url"), "grpc": ("address",)}.get(kind, ())
    out = [str(connection[k]) for k in keys if connection.get(k)]
    if credential and credential.get("token_url"):
        out.append(str(credential["token_url"]))
    return out


@dataclass(frozen=True)
class UpstreamSpec:
    kind: str
    #: 不含凭据。mcp：url、transport；rest：base_url、spec_url；grpc：address、tls、discovery
    connection: Mapping[str, Any]
    credential: UpstreamCredential = field(
        default_factory=lambda: UpstreamCredential.of(None, None)
    )
    #: gRPC：FileDescriptorSet。发现时给了就不反射；调用只用它
    descriptor: bytes | None = None

    def targets(self) -> list[str]:
        return upstream_targets(self.kind, self.connection, self.credential.fields)


@dataclass(frozen=True)
class ToolBinding:
    name: str
    #: rest：method、path、params、pick、limit_kb；grpc：method、stream、request、response
    mapping: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamAuth:
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass
class Discovered:
    """发现的结果。

    mcp    tools：上游 tools/list 的全部工具，MCP 线上格式、原样（全部登记，由服务目录规范化）
    rest   operations：OpenAPI 的操作（管理员挑）；没给 OpenAPI 地址时为空（手工添加）
    grpc   methods：描述里的方法（管理员挑）；descriptor：要存库的 FileDescriptorSet
    """

    kind: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)
    methods: list[Method] = field(default_factory=list)
    descriptor: bytes | None = None
    services: list[str] = field(default_factory=list)
