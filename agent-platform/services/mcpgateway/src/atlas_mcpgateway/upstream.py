"""上游（本期是 Higress 上的服务，§3.2）：地址、网关凭据、拉工具定义、接入检查。

★ 上游只认网关凭据（§8.3）：本服务调上游一律带上它；凭据只在本服务和 Higress 里。
★ 接入检查：不带网关凭据访问上游必须被拒。能访问通就说明 Higress 上这个服务漏开了认证 ——
  Pod 能绕过网关直接调它，登记时不允许，刷新时复查并告警。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .settings import GatewaySettings

logger = logging.getLogger(__name__)

__all__ = [
    "AccessCheck",
    "Upstream",
    "describe_error",
    "root_causes",
]

#: 接入检查发的 initialize（不带任何凭据）
_PROBE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "mcp-gateway-access-check", "version": "1"},
    },
}


def root_causes(exc: BaseException) -> list[BaseException]:
    """真正的原因：ExceptionGroup 展开到叶子，再顺着显式的 __cause__ 往下走
    （FastMCP 把连接失败包成 RuntimeError … from ConnectError）。

    ★ cause 是 CancelledError 时停下：asyncio.timeout 抛的 TimeoutError 就是 from CancelledError。
    """
    if isinstance(exc, BaseExceptionGroup):
        return [root for sub in exc.exceptions for root in root_causes(sub)]
    cause = exc.__cause__
    if cause is not None and not isinstance(cause, asyncio.CancelledError):
        return root_causes(cause)
    return [exc]


def describe_error(exc: BaseException) -> str:
    root = root_causes(exc)[0]
    text = str(root) or type(root).__name__
    return f"{type(root).__name__}: {text}"[:500]


@dataclass(frozen=True)
class AccessCheck:
    #: True = 不带凭据被拒（通过）；False = 不带凭据也能调通；None = 没能判断（连不上、其它状态码）
    ok: bool | None
    detail: str


class Upstream:
    """一个上游服务的地址与调用方式。"""

    def __init__(self, settings: GatewaySettings, upstream_name: str) -> None:
        self.url = f"{settings.higress_base_url.rstrip('/')}/mcp-servers/{upstream_name}"
        self._settings = settings

    def credential_headers(self) -> dict[str, str]:
        """网关凭据。★ 没配置就明确失败：不能不带凭据去调，那等于把请求交给一个没认证的上游。"""
        credential = self._settings.gateway_credential
        if credential is None or not credential.get_secret_value():
            raise RuntimeError("没有配置网关凭据（ATLAS_MCPGW_GATEWAY_CREDENTIAL）")
        return {"Authorization": f"Bearer {credential.get_secret_value()}"}

    async def list_tools(self) -> list[dict[str, Any]]:
        """带网关凭据拉一次完整的工具列表（含分页）→ MCP 线上格式的工具定义。

        ★ mode="legacy"：Higress 2.2.4 只懂 initialize 握手（与 Atlas 的客户端同一条理由）。
        ★ 分页上限 100 页：超过即报错（刷新失败、保留旧定义），不截断成半份目录。
        """
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(self.url, headers=self.credential_headers())
        async with asyncio.timeout(self._settings.discovery_timeout_s):
            async with Client(transport, mode="legacy") as client:
                tools = await client.list_tools(max_pages=100)
        return [t.model_dump(by_alias=True, exclude_none=True) for t in tools]

    async def check_access(self) -> AccessCheck:
        """不带网关凭据发一个 initialize：401 / 403 = 通过。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.discovery_timeout_s, trust_env=False
            ) as http:
                r = await http.post(
                    self.url,
                    json=_PROBE,
                    headers={"Accept": "application/json, text/event-stream"},
                )
        except httpx.HTTPError as exc:
            return AccessCheck(None, f"上游连不上：{describe_error(exc)}")
        if r.status_code in (401, 403):
            return AccessCheck(True, f"不带网关凭据被拒（HTTP {r.status_code}）")
        if r.status_code < 300:
            return AccessCheck(False, "不带网关凭据也能调通：上游没有开启认证，可以绕过 MCP 网关")
        return AccessCheck(None, f"上游返回 HTTP {r.status_code}，无法判断是否开启了认证")
