"""litellm 网关客户端。

litellm 的知识集中在 engine —— server 只调用这里，不自己拼网关 URL。
保持纯粹：接受显式的 base_url / api_key，不读环境变量、不碰 DB。

实测结论（2026-08-19，见 docs/backend-design.md §3）：
  · GET  /v1/models            ✅ 只返回 id，无能力元信息
  · POST /v1/messages          ✅ Anthropic 原生协议，tools / cache_control / thinking 均生效
  · POST /anthropic/v1/messages ❌ 404（nginx 未开放，不要用）
  · GET  /model/info           ❌ 该 key 无 admin 权限
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from atlas_engine.contracts import ModelUnavailable

# GET /v1/models 只给 id，能力元信息必须在 Atlas 侧维护 → server 的 model_catalog 表。
MESSAGES_PATH = "/v1/messages"
MODELS_PATH = "/v1/models"


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str
    timeout_s: float = 120.0


async def list_models(cfg: GatewayConfig) -> list[str]:
    """网关当前可用的模型 id 列表。用于给 model_catalog 做可用性巡检。"""
    url = f"{cfg.base_url.rstrip('/')}{MODELS_PATH}"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        raise ModelUnavailable(
            f"网关 {MODELS_PATH} 返回 {exc.response.status_code}",
            status=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise ModelUnavailable(f"网关不可达：{exc}") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise ModelUnavailable(f"{MODELS_PATH} 返回结构异常，缺少 data 数组")

    return sorted(str(item["id"]) for item in data if isinstance(item, dict) and item.get("id"))
