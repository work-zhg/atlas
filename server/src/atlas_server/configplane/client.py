"""配置服务的 HTTP 客户端。"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import CapabilityUnavailable

__all__ = ["ConfigClient", "ConfigNotFound", "ConfigPlaneUnavailable"]

#: 配置服务在内网，连不上就该快速失败 —— 调用方都有缓存兜底
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class ConfigPlaneUnavailable(CapabilityUnavailable):
    """配置服务不可用，且没有可用的缓存。消息面向用户（run.failed 会原样展示）。"""


class ConfigNotFound(LookupError):
    """配置服务明确回答了 404 —— 与「连不上」不同，不该用缓存兜底。"""


class ConfigClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        #: 测试注入（MockTransport / 配置服务的 ASGI app）；生产为 None
        self._transport = transport

    async def get(self, path: str, **params: Any) -> Any:
        try:
            # ★ trust_env=False：配置服务在内网，绝不该经开发机的代理绕一圈
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, trust_env=False, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._base}{path}", params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ConfigPlaneUnavailable(f"配置服务不可用（{type(exc).__name__}）") from exc
        if resp.status_code == 404:
            raise ConfigNotFound(path)
        if resp.status_code >= 400:
            raise ConfigPlaneUnavailable(f"配置服务返回 {resp.status_code}：{resp.text[:200]}")
        return resp.json()
