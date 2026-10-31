"""MCP 网关上的一个服务，以及连它的 MCP 客户端（doc/mcp-design.html 第二版）。

★ Atlas 和 Pod 只认 MCP 网关（§3.1）：服务地址都是 {MCP_GATEWAY_URL}/mcp/{服务名}，看不到上游。
  凭据只有这个智能体的 MCP Key（session.KeyAuth），上游凭据在网关。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from ...domain.mcp_naming import SERVER_NAME_PATTERN

ResponseHook = Callable[[Any], Awaitable[None]]


class McpServerConfig(BaseModel):
    """MCP 网关上的一个服务（由网关的元数据得出，gateway.GatewayCatalog）。

    ★ name 不许含下划线：模型侧名是 `<server>__<tool>`，server 名里有
      `_` 的话分隔点就不唯一了，审计时无法从模型侧名还原来源（MCP 详设 §04）。
    """

    name: str = Field(pattern=SERVER_NAME_PATTERN)
    #: 网关上的地址
    url: str
    enabled: bool = True
    #: 本 server 单次 tools/call 的上限，None = 用全局 MCP_CALL_TIMEOUT_S。
    #: ★ 按 server 配而不是只有全局：各家延迟分布差得很远 —— serpapi 对没有
    #:   缓存的查询偶尔 50s+，全局调到它能接受的值，会让其它 server 挂住时
    #:   模型陪着干等同样久。
    call_timeout_s: float | None = Field(default=None, gt=0)
    #: 工具定义是否要人工复核后才进模型上下文（网关登记服务时定）
    review_required: bool = False


def client_for(
    server: McpServerConfig,
    *,
    headers: Mapping[str, str] | None = None,
    on_response: ResponseHook | None = None,
    auth: Any = None,
) -> Any:
    """一个 FastMCP client（Streamable HTTP）。

    headers：身份请求头（审计用，网关会改写成它确认过的身份）。
    on_response：挂到底层 httpx2 client 的响应钩子（native.py 用它记 401/403、会话失效）。
    auth：httpx2.Auth —— 主体 Key（session.KeyAuth）。每个请求现取，Key 被重置后换一个值，
          已打开的会话不用重建（§6.7 ④）。

    ★ mode="legacy"：沿用 initialize 握手（当初的上游 Higress 2.2.4 只懂它）。现在网关自己是 MCP
      服务端（SDK 2.3），两种握手都支持；改回 "auto" 要先实测请求数再定（§8.2）。
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        server.url,
        headers=dict(headers or {}),
        auth=auth,
        httpx_client_factory=_client_factory(on_response),
    )
    return Client(transport, mode="legacy")


#: 测试替换用（生产为 None，走真实网络）：例如经 httpx2.ASGITransport 连进程内的 MCP 网关
_transport: Any = None


def _client_factory(hook: ResponseHook | None) -> Callable[..., Any] | None:
    """httpx2 client 工厂：默认配置不变，只多挂一个响应钩子。

    ★ 用 SDK 自己的 create_mcp_http_client 建（FastMCP 不传工厂时也是它），超时等
      默认值不在这里抄一份。FastMCP 会多传 follow_redirects，SDK 那条路径同样不用它。
    """
    if hook is None and _transport is None:
        return None
    from mcp.shared._httpx_utils import create_mcp_http_client

    def make(headers: Any = None, timeout: Any = None, auth: Any = None, **_: Any) -> Any:
        if _transport is not None:
            import httpx2

            client = httpx2.AsyncClient(
                transport=_transport, headers=headers, timeout=timeout, auth=auth
            )
        else:
            client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        if hook is not None:
            client.event_hooks.setdefault("response", []).append(hook)
        return client

    return make
