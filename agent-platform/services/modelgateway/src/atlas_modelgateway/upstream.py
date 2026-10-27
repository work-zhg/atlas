"""上游（本期是 Higress 上的 AI 路由，§9.5）：地址、网关凭据、接入检查。

★ 转发到 {higress_base_url}{同一个路径}：Higress 按请求体里的模型名（换成上游模型名之后）
  选 AI 路由，
  再换成供应商 Key 调供应商，必要时转换协议。
★ 上游只认网关凭据（§9.3）：本服务调上游一律带上它；凭据只在本服务和 Higress 里。
★ 接入检查：不带网关凭据发一个最小请求，必须被拒。能调通就说明 Higress 上这条路由漏开了认证 ——
  Pod 能绕过网关直接调它，登记时不允许，探测时复查并告警。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .settings import GatewaySettings

logger = logging.getLogger(__name__)

__all__ = ["PATHS", "AccessCheck", "Upstream", "describe_error"]

#: 协议 → 这一类调用的路径（对话按协议分；嵌入、重排各一个）
PATHS: dict[tuple[str, str], str] = {
    ("chat", "anthropic"): "/v1/messages",
    ("chat", "openai"): "/v1/chat/completions",
    ("embedding", "openai"): "/v1/embeddings",
    ("rerank", "rerank"): "/v1/rerank",
}


def describe_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}"[:500]


def minimal_body(kind: str, protocol: str, model: str) -> dict[str, Any]:
    """接入检查、探测用的最小请求体。"""
    if kind == "embedding":
        return {"model": model, "input": ["ping"]}
    if kind == "rerank":
        return {"model": model, "query": "ping", "documents": ["ping"]}
    if protocol == "anthropic":
        return {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    return {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}


@dataclass(frozen=True)
class AccessCheck:
    #: True = 不带凭据被拒（通过）；False = 不带凭据也能调通；None = 没能判断（连不上、其它状态码）
    ok: bool | None
    detail: str


class Upstream:
    """上游的地址与调用方式（不分模型：Higress 按请求体里的模型名路由）。"""

    def __init__(self, settings: GatewaySettings) -> None:
        self.base_url = settings.higress_base_url.rstrip("/")
        self._settings = settings

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def credential_headers(self) -> dict[str, str]:
        """网关凭据。★ 没配置就明确失败：不能不带凭据去调，那等于把请求交给一个没认证的上游。"""
        credential = self._settings.gateway_credential
        if credential is None or not credential.get_secret_value():
            raise RuntimeError("没有配置网关凭据（ATLAS_MODELGW_GATEWAY_CREDENTIAL）")
        return {"Authorization": f"Bearer {credential.get_secret_value()}"}

    async def check_access(self, kind: str, protocol: str, upstream_model: str) -> AccessCheck:
        """不带网关凭据发一个最小请求：401 / 403 = 通过。"""
        path = PATHS[(kind, protocol)]
        headers = {"Content-Type": "application/json"}
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.probe_timeout_s, trust_env=False
            ) as http:
                r = await http.post(
                    self.url(path),
                    json=minimal_body(kind, protocol, upstream_model),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return AccessCheck(None, f"上游连不上：{describe_error(exc)}")
        if r.status_code in (401, 403):
            return AccessCheck(True, f"不带网关凭据被拒（HTTP {r.status_code}）")
        if r.status_code < 300:
            return AccessCheck(False, "不带网关凭据也能调通：上游没有开启认证，可以绕过模型网关")
        return AccessCheck(None, f"上游返回 HTTP {r.status_code}，无法判断是否开启了认证")
