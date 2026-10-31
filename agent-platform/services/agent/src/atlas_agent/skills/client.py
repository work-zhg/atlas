"""技能应用的 HTTP 客户端。内网免令牌：带本次调用的身份请求头（内网身份传递 §3）。"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import CapabilityUnavailable
from ..outbound import outbound_headers

__all__ = ["SkillClient", "SkillServiceNotFound", "SkillServiceUnavailable"]

#: 技能应用在内网，连不上就该快速失败 —— 调用方都有缓存兜底
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class SkillServiceUnavailable(CapabilityUnavailable):
    """技能应用不可用，且没有可用的缓存。消息面向用户（run.failed 会原样展示）。"""


class SkillServiceNotFound(LookupError):
    """技能应用明确回答了 404 —— 与「连不上」不同，不该用缓存兜底。"""


class SkillClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = base_url.rstrip("/")
        #: 测试注入（MockTransport / 技能应用的 ASGI app）；生产为 None
        self._transport = transport

    async def get(self, path: str, **params: Any) -> Any:
        try:
            # ★ trust_env=False：技能应用在内网，绝不该经开发机的代理绕一圈
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, trust_env=False, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{self._base}{path}", params=params, headers=outbound_headers()
                )
        except httpx.HTTPError as exc:
            raise SkillServiceUnavailable(f"技能应用不可用（{type(exc).__name__}）") from exc
        if resp.status_code == 404:
            raise SkillServiceNotFound(path)
        if resp.status_code >= 400:
            raise SkillServiceUnavailable(f"技能应用返回 {resp.status_code}：{resp.text[:200]}")
        return resp.json()
