"""模型管理平台（agent-platform/doc/design/model-design.html）在 Atlas 这一侧。

    客户端 Key   每个智能体一把：客户端类型 Agent、客户端编码 = 智能体 id。建智能体时创建（幂等），
                 平台接入前建的智能体在编辑器、Run 开始时补建。
                 创建后「待分配」，管理员分配模型后可用
    可用模型     Key 的有效模型（平台算好的）：编辑器的下拉框、保存时的校验、Run 开始的检查都按它
    端点         这一次模型调用连哪、带什么 Key：
                 经平台 = LiteLLM + 客户端 Key；没接平台 = 直连 litellm
    检查         Run 开始（Native）、建会话（ACP）时：Key 可用、本轮用到的模型都在可用模型里

★ 调 LiteLLM 一律用 Anthropic Messages：非 Anthropic 的模型由 LiteLLM 转换
  （ACP 的 Claude Code 也只认
  这一种）。思考、档位、温度等参数仍按 domain/spec.py 的规则只发给支持的模型。
★ 平台暂时不可用：沿用这个进程上次取到的 Key 和可用模型
  （最终由 LiteLLM 校验，旧 Key 失效会被拒绝）；
  从没取到过的才报暂时不可用。
★ 模型能用哪些与智能体的权限类型无关：同一个智能体对所有使用者一样。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

import httpx

from ...errors import CapabilityUnavailable
from ...outbound import outbound_headers

if TYPE_CHECKING:
    from ...config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_CLIENT",
    "ClientKey",
    "ModelEndpoint",
    "ModelNotAllowed",
    "ModelPlatformClient",
    "ModelPlatformUnavailable",
    "PlatformModel",
    "admit_models",
    "agent_key",
    "client_key",
    "direct_endpoint",
    "key_endpoint",
    "platform_catalog",
    "register_agent",
    "rename_agent",
    "reset_model_platform_cache",
    "uses_model_platform",
    "with_platform_model",
]

#: 智能体的客户端类型（平台的 ATLAS_MODELGW_CLIENT_TYPES 里登记给应用 atlas）
AGENT_CLIENT = "Agent"


class ModelPlatformUnavailable(CapabilityUnavailable):
    """没接模型管理平台、连不上，或平台回了错误。"""


def uses_model_platform(settings: Settings) -> bool:
    return bool(settings.model_platform_url)


def litellm_url(settings: Settings) -> str:
    """经平台时模型调用连的 LiteLLM。没单独配就用 litellm_base_url。"""
    return settings.model_litellm_url or str(settings.litellm_base_url)


# ───────────────────────────── 平台给的数据


@dataclass(frozen=True)
class PlatformModel:
    """平台上登记、可调用的一个模型（规格来自 LiteLLM）。"""

    name: str
    display_name: str
    provider: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> PlatformModel:
        return cls(
            name=str(raw["name"]),
            display_name=str(raw.get("display_name") or raw["name"]),
            provider=str(raw.get("provider") or ""),
            max_input_tokens=raw.get("max_input_tokens"),
            max_output_tokens=raw.get("max_output_tokens"),
        )


@dataclass(frozen=True)
class ClientKey:
    """一把客户端 Key 此刻的样子。

    status  pending_models  刚创建、还没分配模型
            active          可用：key 是原文，models 是有效模型
            blocked         被管理员停用
            no_models       分配的模型都不可用了（被删、LiteLLM 中缺失）
    """

    client_type: str
    client_code: str
    name: str
    status: str
    key: str | None = None
    models: tuple[PlatformModel, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return self.status == "active" and bool(self.key)

    def get(self, name: str) -> PlatformModel | None:
        return next((m for m in self.models if m.name == name), None)

    def allows(self, name: str) -> bool:
        return self.get(name) is not None

    def problem(self) -> str | None:
        """不可用的原因（给用户看）；可用为 None。"""
        who = f"智能体「{self.name}」" if self.client_type == AGENT_CLIENT else f"「{self.name}」"
        return {
            "pending_models": f"{who}还没有分配模型，请联系模型管理员在模型管理平台分配",
            "blocked": f"{who}的模型 Key 已被停用，请联系模型管理员",
            "no_models": f"{who}分配的模型都不可用了（已删除或暂时下线），请联系模型管理员重新分配",
        }.get(self.status)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> ClientKey:
        return cls(
            client_type=str(raw["client_type"]),
            client_code=str(raw["client_code"]),
            name=str(raw.get("name") or raw["client_code"]),
            status=str(raw["status"]),
            key=raw.get("key"),
            models=tuple(PlatformModel.parse(m) for m in raw.get("models") or ()),
        )


class ModelPlatformClient:
    """平台的内部接口（/internal/*）。内网免令牌，X-Caller-App 随出站身份带上。"""

    #: 测试替换用（生产为 None，走真实网络）
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None

    def __init__(self, settings: Settings) -> None:
        if not settings.model_platform_url:
            raise ModelPlatformUnavailable("没有配置模型管理平台（MODEL_PLATFORM_URL）")
        self.base_url = settings.model_platform_url.rstrip("/")
        self._timeout = settings.model_platform_timeout_s

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """→ 响应体；404 为 None。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=ModelPlatformClient.transport
            ) as http:
                r = await http.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    params=params,
                    headers=outbound_headers(),
                )
        except httpx.HTTPError as exc:
            raise ModelPlatformUnavailable(f"模型管理平台暂时不可用：{type(exc).__name__}") from exc
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ModelPlatformUnavailable(f"模型管理平台返回 HTTP {r.status_code}：{detail}")
        return r.json()

    async def ensure(self, client_type: str, client_code: str, name: str) -> ClientKey:
        body = {"client_type": client_type, "client_code": client_code, "name": name[:200]}
        found = await self._call("POST", "/internal/client-keys", json=body)
        if found is None:
            raise ModelPlatformUnavailable(f"模型管理平台不认识客户端类型 {client_type}")
        return ClientKey.parse(found)

    async def get(self, client_type: str, client_code: str) -> ClientKey | None:
        found = await self._call(
            "GET",
            "/internal/client-keys",
            params={"client_type": client_type, "client_code": client_code},
        )
        return ClientKey.parse(found) if found is not None else None

    async def rename(self, client_type: str, client_code: str, name: str) -> None:
        body = {"client_type": client_type, "client_code": client_code, "name": name[:200]}
        await self._call("PATCH", "/internal/client-keys", json=body)

    async def catalog(self) -> list[PlatformModel]:
        found = await self._call("GET", "/internal/models")
        return [PlatformModel.parse(m) for m in (found or {}).get("models") or ()]


#: (客户端类型, 编码) → 上次取到的。平台暂时不可用时沿用
_last: dict[tuple[str, str], ClientKey] = {}
_last_catalog: list[PlatformModel] | None = None


def reset_model_platform_cache() -> None:
    global _last_catalog
    _last.clear()
    _last_catalog = None


async def client_key(
    settings: Settings, client_type: str, client_code: str, *, ensure_name: str | None = None
) -> ClientKey | None:
    """取最新的客户端 Key。ensure_name：平台上还没有就以这个名字补建（智能体用）。

    → None：平台上没有、也没让补建。
    平台不可用时沿用上次取到的，从没取到过抛 ModelPlatformUnavailable。
    """
    slot = (client_type, client_code)
    try:
        client = ModelPlatformClient(settings)
        found = await client.get(client_type, client_code)
        if found is None and ensure_name is not None:
            found = await client.ensure(client_type, client_code, ensure_name)
    except ModelPlatformUnavailable:
        cached = _last.get(slot)
        if cached is None:
            raise
        logger.warning("模型管理平台不可用，沿用上次取到的客户端 Key（%s:%s）", *slot)
        return cached
    if found is not None:
        if len(_last) > 10_000:
            _last.clear()
        _last[slot] = found
    else:
        _last.pop(slot, None)
    return found


async def agent_key(settings: Settings, agent_id: UUID | str, name: str) -> ClientKey:
    """智能体的客户端 Key（没有就补建，「待分配」）。"""
    found = await client_key(settings, AGENT_CLIENT, str(agent_id), ensure_name=name)
    assert found is not None
    return found


async def register_agent(settings: Settings, agent_id: UUID | str, name: str) -> None:
    """建智能体时创建客户端 Key。

    ★ 尽力而为：平台暂时不可用不挡住建智能体，编辑器、Run 开始时会补建。
    """
    if not uses_model_platform(settings):
        return
    try:
        await ModelPlatformClient(settings).ensure(AGENT_CLIENT, str(agent_id), name)
    except ModelPlatformUnavailable as exc:
        logger.warning(
            "建智能体 %s 时没能在模型管理平台创建客户端 Key（之后补建）：%s", agent_id, exc
        )


async def rename_agent(settings: Settings, agent_id: UUID | str, name: str) -> None:
    """智能体改名：同步客户端 Key 的名字（只影响平台页面上的展示，尽力而为）。"""
    if not uses_model_platform(settings):
        return
    try:
        await ModelPlatformClient(settings).rename(AGENT_CLIENT, str(agent_id), name)
    except ModelPlatformUnavailable as exc:
        logger.info("同步客户端 Key 的名字失败（不影响使用）：%s", exc)


async def platform_catalog(settings: Settings) -> list[PlatformModel]:
    """平台上已登记、可调用的全部模型（客户端 Key 还没分配时编辑器先列它们）。"""
    global _last_catalog
    try:
        _last_catalog = await ModelPlatformClient(settings).catalog()
    except ModelPlatformUnavailable:
        if _last_catalog is None:
            raise
        logger.warning("模型管理平台不可用，沿用上次取到的模型列表")
    return _last_catalog


# ───────────────────────────── 端点与检查


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    api_key: str
    headers: Mapping[str, str] = field(default_factory=dict)


def direct_endpoint(settings: Settings) -> ModelEndpoint:
    """没接模型管理平台：直连 litellm，平台的一把 Key。"""
    return ModelEndpoint(str(settings.litellm_base_url), settings.litellm_key.get_secret_value())


def key_endpoint(settings: Settings, key: str) -> ModelEndpoint:
    """经平台：LiteLLM + 客户端 Key。"""
    return ModelEndpoint(litellm_url(settings), key)


class ModelNotAllowed(CapabilityUnavailable):
    """这一轮用到的模型用不了：Key 待分配、被停用，或模型不在可用模型里。

    ★ 和 MCP 工具不同：工具缺一个可以跳过，模型缺了这一轮就做不了 —— 本轮不开始，说明缺的是
      哪个模型、该找谁。
    """


def admit_models(key: ClientKey, names: Sequence[str]) -> None:
    """Run 开始（Native）、建会话（ACP）时的检查。用不了抛 ModelNotAllowed（把问题一起说清楚）。"""
    problem = key.problem()
    if problem is not None:
        raise ModelNotAllowed(problem)
    missing = [n for n in dict.fromkeys(names) if not key.allows(n)]
    if missing:
        who = f"智能体「{key.name}」" if key.client_type == AGENT_CLIENT else f"「{key.name}」"
        raise ModelNotAllowed(
            f"{who}没有分配模型 {', '.join(missing)}，请联系模型管理员分配，或换一个已分配的模型"
        )
    if not key.key:
        raise ModelNotAllowed("暂时取不到模型 Key，请稍后重试")


def with_platform_model(model: Any) -> Any:
    """模型规格换成经平台的调用方式：一律 Anthropic Messages（LiteLLM 负责转换）。"""
    return replace(model, provider="anthropic")
