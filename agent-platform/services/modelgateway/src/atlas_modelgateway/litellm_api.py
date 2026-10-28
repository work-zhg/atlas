"""LiteLLM 管理接口的客户端（模型管理方案设计第 2、3、6 章）。

平台只调 LiteLLM 的管理接口（带 master key），不转发模型调用 —— 应用拿着 Key 直连 LiteLLM。

    models          列出 LiteLLM 里的模型部署（合并展示、算有效模型）
    add_model       登记模型：model_info.id 用平台模型的 uuid，之后改、删都按它
    update_model    改地址、凭据、规格价格（PATCH，按字段合并：凭据不传 = 不改）
    delete_model
    test_connection 用填写的参数试调一次（登记前）
    generate_key    生成 Key（返回原文和 LiteLLM 里的标识）
    update_key      改可用模型
    block_key / unblock_key / delete_key / key_info

★ LiteLLM 里 Key 的 models 为空表示「全部模型」：update_key、generate_key 拒绝空列表，
  调用方（keys.py）在有效模型为空时改为停用这把 Key。
★ LiteLLM 的模型名就是平台的模型名（调用时传的 model）；litellm_params.model 写成
  「供应商/模型名」，并显式带上 custom_llm_provider。
★ 放在哪（LiteLLM 1.106 实测）：上下文窗口、最大输出放 model_info；自定义价格放 litellm_params
  （放 model_info 不生效，/model/info 里显示的是它合并后的结果）。
  修改要用 PATCH /model/{id}/update：POST /model/update 不更新 model_info。
★ Key 删除是软删除：/key/info 还查得到，status 是 deleted（key_info 按「没有」返回）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import Invalid, NotFound, Unavailable

logger = logging.getLogger(__name__)

__all__ = ["Deployment", "GeneratedKey", "LiteLLM", "litellm_params"]


@dataclass(frozen=True)
class Deployment:
    """LiteLLM 里的一个模型部署（GET /model/info 的一项）。"""

    id: str
    model_name: str
    provider: str
    api_base: str
    max_input_tokens: int | None
    max_output_tokens: int | None
    #: 美元 / token
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    #: True = 存在 LiteLLM 库里（经接口或管理界面加的）；False = 配置文件里的
    db_model: bool

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Deployment:
        params = raw.get("litellm_params") or {}
        info = raw.get("model_info") or {}
        return cls(
            id=str(info.get("id") or ""),
            model_name=str(raw.get("model_name") or ""),
            provider=str(params.get("custom_llm_provider") or info.get("litellm_provider") or ""),
            api_base=str(params.get("api_base") or ""),
            max_input_tokens=_int(info.get("max_input_tokens")),
            max_output_tokens=_int(info.get("max_output_tokens") or info.get("max_tokens")),
            input_cost_per_token=_float(info.get("input_cost_per_token")),
            output_cost_per_token=_float(info.get("output_cost_per_token")),
            db_model=bool(info.get("db_model")),
        )


@dataclass(frozen=True)
class GeneratedKey:
    #: 原文（sk-…）
    key: str
    #: LiteLLM 里的标识（哈希）；对账、在 LiteLLM 管理界面里对得上
    token: str | None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def litellm_params(
    *,
    model_name: str,
    provider: str,
    api_base: str | None,
    api_key: str | None,
    pricing: dict[str, float] | None = None,
) -> dict[str, Any]:
    """登记、试调、修改时的 litellm_params。api_base / api_key 为空就不带（用供应商默认、或不改）；
    pricing：input_cost_per_token / output_cost_per_token（美元 / token）。"""
    params: dict[str, Any] = {"model": f"{provider}/{model_name}", "custom_llm_provider": provider}
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key
    params.update(pricing or {})
    return params


class LiteLLM:
    def __init__(
        self,
        base_url: str,
        master_key: str | None,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._master_key = master_key
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, trust_env=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._master_key:
            raise Unavailable("没有配置 LiteLLM 的 master key", code="LITELLM_NOT_CONFIGURED")
        try:
            r = await self._http.request(
                method,
                path,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self._master_key}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("LiteLLM 不可达：%s %s：%s", method, path, exc)
            raise Unavailable(
                f"LiteLLM 暂时不可用：{type(exc).__name__}", code="LITELLM_UNAVAILABLE"
            ) from exc
        if r.status_code < 400:
            return r.json() if r.content else None
        message = _error_message(r)
        if r.status_code == 404:
            raise NotFound(f"LiteLLM：{message}", code="LITELLM_NOT_FOUND")
        if r.status_code in (400, 422):
            raise Invalid(f"LiteLLM 拒绝了请求：{message}", code="LITELLM_REJECTED")
        logger.warning("LiteLLM 返回 %s：%s %s：%s", r.status_code, method, path, message)
        raise Unavailable(f"LiteLLM 返回 HTTP {r.status_code}：{message}", code="LITELLM_ERROR")

    # ── 模型

    async def models(self) -> list[Deployment]:
        body = await self._call("GET", "/model/info")
        return [Deployment.parse(item) for item in (body or {}).get("data") or []]

    async def add_model(
        self,
        *,
        model_id: str,
        model_name: str,
        provider: str,
        api_base: str | None,
        api_key: str | None,
        info: dict[str, Any],
        pricing: dict[str, float] | None = None,
    ) -> None:
        await self._call(
            "POST",
            "/model/new",
            json={
                "model_name": model_name,
                "litellm_params": litellm_params(
                    model_name=model_name,
                    provider=provider,
                    api_base=api_base,
                    api_key=api_key,
                    pricing=pricing,
                ),
                "model_info": {**info, "id": model_id},
            },
        )

    async def update_model(
        self,
        *,
        model_id: str,
        model_name: str,
        provider: str,
        api_base: str | None,
        api_key: str | None,
        info: dict[str, Any],
        pricing: dict[str, float] | None = None,
    ) -> None:
        """api_base / api_key 为空 = 不改（PATCH 按字段合并）。"""
        await self._call(
            "PATCH",
            f"/model/{model_id}/update",
            json={
                "model_name": model_name,
                "litellm_params": litellm_params(
                    model_name=model_name,
                    provider=provider,
                    api_base=api_base,
                    api_key=api_key,
                    pricing=pricing,
                ),
                "model_info": info,
            },
        )

    async def delete_model(self, model_id: str) -> None:
        await self._call("POST", "/model/delete", json={"id": model_id})

    async def test_connection(
        self, *, model_name: str, provider: str, api_base: str | None, api_key: str | None
    ) -> tuple[bool, str]:
        """→ (成功, 说明)。LiteLLM 不可达照常抛 Unavailable。"""
        try:
            body = await self._call(
                "POST",
                "/health/test_connection",
                json={
                    "litellm_params": litellm_params(
                        model_name=model_name, provider=provider, api_base=api_base, api_key=api_key
                    ),
                    "mode": "chat",
                },
            )
        except Invalid as exc:
            return False, exc.message
        body = body or {}
        if str(body.get("status") or "").lower() == "success":
            return True, "连接成功"
        result = body.get("result")
        error = result.get("error") if isinstance(result, dict) else body.get("message")
        # 原因后面跟着整段堆栈（LiteLLM 1.106），只留原因
        reason = str(error or "连接失败").split("\nstack trace:")[0]
        return False, reason[:500]

    # ── Key

    async def generate_key(
        self,
        *,
        models: list[str],
        alias: str,
        metadata: dict[str, Any],
        user_id: str | None = None,
    ) -> GeneratedKey:
        if not models:
            raise Invalid("至少要有一个模型（LiteLLM 里空列表表示全部模型）")
        payload: dict[str, Any] = {"models": models, "key_alias": alias, "metadata": metadata}
        if user_id:
            payload["user_id"] = user_id
        body = await self._call("POST", "/key/generate", json=payload) or {}
        key = body.get("key")
        if not key:
            raise Unavailable("LiteLLM 没有返回 Key", code="LITELLM_ERROR")
        token = body.get("token") or body.get("token_id")
        return GeneratedKey(key=str(key), token=str(token) if token else None)

    async def update_key(self, key: str, *, models: list[str]) -> None:
        if not models:
            raise Invalid("至少要有一个模型（LiteLLM 里空列表表示全部模型）")
        await self._call("POST", "/key/update", json={"key": key, "models": models})

    async def block_key(self, key: str) -> None:
        await self._call("POST", "/key/block", json={"key": key})

    async def unblock_key(self, key: str) -> None:
        await self._call("POST", "/key/unblock", json={"key": key})

    async def delete_key(self, key: str) -> None:
        try:
            await self._call("POST", "/key/delete", json={"keys": [key]})
        except NotFound:
            logger.info("LiteLLM 里已经没有这把 Key，当作已删除")

    async def key_info(self, key: str) -> dict[str, Any] | None:
        """→ {models, blocked, …}；LiteLLM 里没有这把 Key 为 None。"""
        try:
            body = await self._call("GET", "/key/info", params={"key": key})
        except NotFound:
            return None
        info = (body or {}).get("info")
        if not isinstance(info, dict) or info.get("status") == "deleted":
            return None
        return dict(info)


def _error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    if isinstance(body, dict) and body.get("detail"):
        detail = body["detail"]
        if isinstance(detail, dict):
            detail = detail.get("error") or detail
        return str(detail)[:300]
    return str(body)[:300]
