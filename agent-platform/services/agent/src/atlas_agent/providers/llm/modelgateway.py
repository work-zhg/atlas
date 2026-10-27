"""模型网关（doc/model-design.html）在 Atlas 这一侧：模型目录、主体能用什么、调用端点。

    目录          模型的类型、协议、能力、上限、生命周期、价格；平台用途。网关是事实源：
                  进程内短时缓存 + Redis 里留一份，网关暂时不可用时编辑器、装配照样能读（§5.1）
    主体能用什么   主体此刻能用哪些对话模型 + 主体的 Key（网关自动生成，§6.5）。进程内短时缓存，
                  ★ 不进 Redis、不进日志：Key 只活在内存和模型客户端里
    端点          这一次模型调用连哪、带什么 Key、带什么请求头：
                    subject_endpoint   Agent 的推理：主体 Key；使用者、Agent、Run 作参考（§7.2）
                    purpose_endpoint   平台功能：应用身份 + 用途，不带 Key（§6.7）
                    direct_endpoint    没接模型网关（过渡期）：照旧直连 litellm 网关

★ 换成模型网关只改这一处：调用方拿 ModelEndpoint 构造模型客户端，不关心连的是谁。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from ...domain.spec import ModelCaps
from ...errors import CapabilityUnavailable
from ...outbound import caller_app, current_identity, outbound_headers

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ...config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "CatalogModel",
    "ModelCatalog",
    "ModelEndpoint",
    "ModelGatewayUnavailable",
    "ModelNotAllowed",
    "PurposeModel",
    "SubjectModels",
    "admit_models",
    "deprecation_notice",
    "direct_endpoint",
    "forget_model_access",
    "model_catalog",
    "model_subject_access",
    "purpose_endpoint",
    "reset_model_gateway_cache",
    "resolve_purpose",
    "subject_endpoint",
    "uses_model_gateway",
]

_REDIS_KEY = "atlas:models:gateway:catalog"
#: Redis 里那份的保留时间：网关长时间不可用时，过了这么久就不再用旧目录
_REDIS_TTL_S = 7 * 24 * 3600
#: 平台用途的调用不带 Key，但 SDK 要求非空：网关见到 X-Model-Purpose 就不看它
_NO_KEY = "purpose"


class ModelGatewayUnavailable(CapabilityUnavailable):
    """没接模型网关、连不上，或网关回了错误。"""


def uses_model_gateway(settings: Settings) -> bool:
    return bool(settings.model_gateway_url)


# ───────────────────────────── 目录


@dataclass(frozen=True)
class CatalogModel:
    name: str
    display_name: str
    kind: str
    provider: str
    protocols: tuple[str, ...]
    capabilities: Mapping[str, Any]
    context_window: int | None
    max_output_tokens: int | None
    status: str
    disabled: bool
    replacement: str | None = None
    retire_at: str | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ("text",)
    data_policy: Mapping[str, Any] = field(default_factory=dict)
    price: Mapping[str, Any] | None = None

    @property
    def usable(self) -> bool:
        """此刻能调：可用或弃用，没停用。"""
        return self.status in ("available", "deprecated") and not self.disabled

    @property
    def selectable(self) -> bool:
        """能在编辑器里新选：弃用的不能新选（§5.3）。"""
        return self.status == "available" and not self.disabled

    def caps(self) -> ModelCaps:
        return ModelCaps.from_catalog(self.capabilities)

    def protocol(self) -> str | None:
        """Atlas 用哪种协议调它：目录里按偏好排序，原生的在前（§5.2）。"""
        return next((p for p in self.protocols if p in ("anthropic", "openai")), None)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> CatalogModel:
        return cls(
            name=str(raw["name"]),
            display_name=str(raw.get("display_name") or raw["name"]),
            kind=str(raw.get("kind") or "chat"),
            provider=str(raw.get("provider") or ""),
            protocols=tuple(raw.get("protocols") or ()),
            capabilities=dict(raw.get("capabilities") or {}),
            context_window=raw.get("context_window"),
            max_output_tokens=raw.get("max_output_tokens"),
            status=str(raw.get("status") or "onboarding"),
            disabled=bool(raw.get("disabled")),
            replacement=raw.get("replacement"),
            retire_at=raw.get("retire_at"),
            description=str(raw.get("description") or ""),
            tags=tuple(raw.get("tags") or ()),
            input_modalities=tuple(raw.get("input_modalities") or ("text",)),
            data_policy=dict(raw.get("data_policy") or {}),
            price=dict(raw["price"]) if raw.get("price") else None,
        )


@dataclass(frozen=True)
class ModelCatalog:
    models: Mapping[str, CatalogModel]
    #: 用途 → {default_model, allowed_models, kind, callable, name}
    purposes: Mapping[str, Mapping[str, Any]]

    def get(self, name: str) -> CatalogModel | None:
        return self.models.get(name)

    def purpose_model(self, code: str) -> str | None:
        purpose = self.purposes.get(code) or {}
        value = purpose.get("default_model")
        return str(value) if value else None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ModelCatalog:
        models = {m.name: m for m in (CatalogModel.parse(x) for x in raw.get("models") or ())}
        purposes = {str(p["code"]): dict(p) for p in raw.get("purposes") or ()}
        return cls(models=models, purposes=purposes)


@dataclass(frozen=True)
class SubjectModels:
    """一个主体此刻能用什么（§6.4 闸门一）。"""

    subject: str
    #: 能用（有「使用」）、可用或弃用、没停用的对话模型
    models: frozenset[str]
    all_models: bool = False
    #: 原文；没有或已停用为 None
    key: str | None = None
    #: active | disabled | none
    key_status: str = "none"

    def allows(self, model: str) -> bool:
        return model in self.models


class ModelGatewayClient:
    """网关的内部接口（/internal/*）。内网免令牌，身份请求头随出站身份透传。"""

    #: 测试替换用（生产为 None，走真实网络）
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    async def _get(self, path: str) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=ModelGatewayClient.transport
            ) as http:
                r = await http.get(f"{self.base_url}{path}", headers=outbound_headers())
        except httpx.HTTPError as exc:
            raise ModelGatewayUnavailable(f"模型网关暂时不可用：{type(exc).__name__}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ModelGatewayUnavailable(f"模型网关返回 HTTP {r.status_code}：{detail}")
        return r.json()

    async def catalog(self) -> dict[str, Any]:
        return dict(await self._get("/internal/catalog"))

    async def access(self, subject: str) -> SubjectModels:
        body = await self._get(f"/internal/subjects/{subject}/access")
        return SubjectModels(
            subject=str(body["subject"]),
            models=frozenset(body.get("models") or []),
            all_models=bool(body.get("all_models")),
            key=body.get("key"),
            key_status=str(body.get("key_status") or "none"),
        )


class _CatalogCache:
    """目录：TTL 内直接用；过期了问网关，问不到就继续用上一份（进程内或 Redis 里的）。"""

    def __init__(self) -> None:
        self.value: ModelCatalog | None = None
        self.at = 0.0

    async def get(
        self, client: ModelGatewayClient, ttl_s: float, redis: aioredis.Redis | None
    ) -> ModelCatalog:
        if self.value is not None and time.monotonic() - self.at < ttl_s:
            return self.value
        try:
            raw = await client.catalog()
            self.value = ModelCatalog.parse(raw)
            await _write_copy(redis, raw)
        except ModelGatewayUnavailable:
            if self.value is None:
                copy = await _read_copy(redis)
                self.value = ModelCatalog.parse(copy) if copy is not None else None
            if self.value is None:
                raise
            logger.warning("模型网关不可用，继续使用缓存的模型目录")
        self.at = time.monotonic()
        return self.value


async def _write_copy(redis: aioredis.Redis | None, value: dict[str, Any]) -> None:
    if redis is None:
        return
    try:
        await redis.set(_REDIS_KEY, json.dumps(value, ensure_ascii=False), ex=_REDIS_TTL_S)
    except Exception:
        logger.warning("写入模型目录缓存失败", exc_info=True)


async def _read_copy(redis: aioredis.Redis | None) -> dict[str, Any] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_REDIS_KEY)
    except Exception:
        logger.warning("读取模型目录缓存失败", exc_info=True)
        return None
    return dict(json.loads(raw)) if raw else None


_catalog = _CatalogCache()
#: 主体 → (过期时刻, 能用什么)
_access: dict[str, tuple[float, SubjectModels]] = {}


def reset_model_gateway_cache() -> None:
    _catalog.value, _catalog.at = None, 0.0
    _access.clear()


def _gateway_url(settings: Settings) -> str:
    if not settings.model_gateway_url:
        raise ModelGatewayUnavailable("没有配置模型网关（MODEL_GATEWAY_URL）")
    return settings.model_gateway_url


async def model_catalog(settings: Settings, redis: aioredis.Redis | None) -> ModelCatalog:
    """模型目录（读缓存）。没接模型网关、网关不可用又没有缓存时抛 ModelGatewayUnavailable。"""
    client = ModelGatewayClient(_gateway_url(settings))
    return await _catalog.get(client, settings.model_catalog_ttl_s, redis)


async def model_subject_access(settings: Settings, subject: UUID | str) -> SubjectModels:
    """主体此刻能用哪些模型、Key 是什么。网关不可用抛 ModelGatewayUnavailable。"""
    key = str(subject)
    row = _access.get(key)
    if row is not None and row[0] >= time.monotonic():
        return row[1]
    found = await ModelGatewayClient(_gateway_url(settings)).access(key)
    ttl = settings.model_access_ttl_s
    if ttl > 0:
        if len(_access) > 10_000:
            _access.clear()
        _access[key] = (time.monotonic() + ttl, found)
    return found


def forget_model_access(subject: UUID | str) -> None:
    """Key 刚被网关拒绝（可能刚重置）：下次重新取（§7.3 ③）。"""
    _access.pop(str(subject), None)


# ───────────────────────────── 端点


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    api_key: str
    headers: Mapping[str, str] = field(default_factory=dict)


def direct_endpoint(settings: Settings) -> ModelEndpoint:
    """没接模型网关（过渡期）：直连 litellm 网关，平台的一把 Key。"""
    return ModelEndpoint(str(settings.litellm_base_url), settings.litellm_key.get_secret_value())


def _reference_headers(**extra: str | None) -> dict[str, str]:
    """只作用量归属参考的请求头：使用者、Agent、Run（网关不拿它们判权，§10.1）。"""
    ident = current_identity()
    headers = {"X-Caller-App": caller_app()}
    if ident is not None:
        headers["X-Uid"] = str(ident.uid)
    headers.update({k: v for k, v in extra.items() if v})
    return headers


def subject_endpoint(
    settings: Settings, key: str, *, agent_id: str | None = None, run_id: str | None = None
) -> ModelEndpoint:
    """Agent 的推理：带这一轮权限主体的 Key（§7.2）。"""
    return ModelEndpoint(
        _gateway_url(settings),
        key,
        _reference_headers(**{"X-Agent-Id": agent_id, "X-Run-Id": run_id}),
    )


def purpose_endpoint(settings: Settings, purpose: str) -> ModelEndpoint:
    """平台功能：应用身份 + 用途，不带 Key（§6.7）。"""
    return ModelEndpoint(
        _gateway_url(settings), _NO_KEY, _reference_headers(**{"X-Model-Purpose": purpose})
    )


class ModelNotAllowed(CapabilityUnavailable):
    """经模型网关时，这一轮用到的模型用不了：无权、停用、下线、不在目录里（§7.1）。

    ★ 和 MCP 工具不同：工具缺一个可以跳过，模型缺了这一轮就做不了 —— 本轮不开始，说明缺的是
      哪个模型、按谁的权限判的、该找谁。
    """


async def admit_models(
    settings: Settings,
    redis: aioredis.Redis | None,
    names: list[str],
    *,
    subject: UUID | str,
    acts_as_agent: bool,
    notices: list[tuple[Any, dict[str, Any]]] | None = None,
    protocol: str | None = None,
) -> tuple[ModelCatalog, SubjectModels]:
    """Run 开始（Native）、建会话（ACP）时的检查：这些模型这一轮的主体能不能用（§6.4 闸门一）。

    用不了抛 ModelNotAllowed（把所有问题一起说清楚）；弃用的照常可用，往 notices 里加一条提示。
    protocol：调用方只会用这种协议（ACP 的 Claude Code 只会 Anthropic Messages）。
    网关不可用抛 ModelGatewayUnavailable。
    """
    from ...domain.events import EventType

    catalog = await model_catalog(settings, redis)
    access = await model_subject_access(settings, subject)
    who = "这个 Agent 的数字员工" if acts_as_agent else "你"
    problems: list[str] = []
    for name in dict.fromkeys(names):
        entry = catalog.get(name)
        if entry is None:
            problems.append(f"模型 {name} 不在模型网关的目录里")
        elif entry.disabled:
            problems.append(f"模型 {name} 已被停用")
        elif entry.status == "retired":
            hint = f"，请改用 {entry.replacement}" if entry.replacement else ""
            problems.append(f"模型 {name} 已下线{hint}")
        elif entry.status == "onboarding":
            problems.append(f"模型 {name} 还在接入中")
        elif protocol is not None and protocol not in entry.protocols:
            problems.append(f"模型 {name} 不支持这种 CLI 的调用协议（{protocol}）")
        elif not access.allows(name):
            problems.append(f"{who}没有被授权使用模型 {name}，请联系模型所有者或模型管理员")
        elif entry.status == "deprecated" and notices is not None:
            notices.append((EventType.MODEL_DEPRECATED, deprecation_notice(entry)))
    if not problems and access.key is None:
        problems.append(
            "模型 Key 已被停用，请联系模型管理员"
            if access.key_status == "disabled"
            else "暂时取不到模型 Key（用户中心里找不到权限主体，或网关暂时不可用）"
        )
    if problems:
        raise ModelNotAllowed("；".join(problems))
    return catalog, access


def deprecation_notice(entry: CatalogModel) -> dict[str, Any]:
    when = f"，计划 {entry.retire_at[:10]} 下线" if entry.retire_at else ""
    instead = f"，替代模型：{entry.replacement}" if entry.replacement else ""
    return {
        "model": entry.name,
        "replacement": entry.replacement,
        "retire_at": entry.retire_at,
        "message": f"模型 {entry.name} 已弃用{when}{instead}；本轮照常运行",
    }


@dataclass(frozen=True)
class PurposeModel:
    """平台功能这一次用哪个模型、怎么连。"""

    model: str
    endpoint: ModelEndpoint
    #: 目录给出的能力与协议；没接模型网关时为空（照旧按写死的规则）
    caps: ModelCaps | None = None
    provider: str = "anthropic"


async def resolve_purpose(
    settings: Settings, redis: aioredis.Redis | None, purpose: str, *, legacy_model: str
) -> PurposeModel | None:
    """平台用途 → 模型。没接模型网关时用部署配置里的模型（legacy_model）直连。

    → None：用途还没配模型、配的模型此刻用不了，或网关不可用（调用方降级，不让功能报错）。
    """
    if not uses_model_gateway(settings):
        return PurposeModel(legacy_model, direct_endpoint(settings))
    try:
        catalog = await model_catalog(settings, redis)
    except ModelGatewayUnavailable as exc:
        logger.warning("平台用途 %s 用不了模型：%s", purpose, exc)
        return None
    name = catalog.purpose_model(purpose)
    entry = catalog.get(name) if name else None
    if entry is None or not entry.usable:
        logger.info("平台用途 %s 还没有可用的模型（配置的是 %s）", purpose, name)
        return None
    return PurposeModel(
        entry.name,
        purpose_endpoint(settings, purpose),
        entry.caps(),
        entry.protocol() or "anthropic",
    )
