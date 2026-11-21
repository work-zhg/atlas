"""模型管理平台（agent-platform/doc/design/model-design.html）在 Atlas 这一侧。

    客户端 Key   每个智能体一把：客户端类型 Agent、客户端编码 = 智能体 id。建智能体时创建（幂等），
                 平台接入前建的智能体在编辑器、Run 开始时补建。
                 创建后「待分配」，管理员分配模型后可用
    可用模型     Key 的有效模型（平台算好的）：编辑器的下拉框、保存时的校验、Run 开始的检查都按它
    端点         这一次模型调用连哪、带什么 Key：模型网关（model_gateway_url）+ 客户端 Key
    检查         Run 开始（Native）、建会话（CLI）时：Key 可用、本轮用到的模型都在可用模型里

★ 调模型网关一律用 Anthropic Messages：线路是别的格式由网关转换（CLI 的 Claude Code 也只认
  这一种）。思考、档位、温度按意图原样发，按上游模型整理参数是网关的事。
★ 平台暂时不可用：沿用这个进程上次取到的 Key 和可用模型
  （最终由模型网关校验，旧 Key 失效会被拒绝）；
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
from atlas_tenancy import TenantScope

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
    "for_session",
    "key_endpoint",
    "platform_catalog",
    "register_agent",
    "rename_agent",
    "reset_model_platform_cache",
    "sees_images",
    "with_platform_model",
]

#: 智能体的客户端类型（平台的 ATLAS_MODELGW_CLIENT_TYPES 里登记给应用 atlas）
AGENT_CLIENT = "Agent"


class ModelPlatformUnavailable(CapabilityUnavailable):
    """模型管理平台连不上，或平台回了错误。"""

    system_fault = True


# ───────────────────────────── 平台给的数据


@dataclass(frozen=True)
class PlatformModel:
    """平台上登记、可调用的一个模型（规格是登记时填的）。"""

    name: str
    display_name: str
    provider: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    #: 登记时标注能看图（图片输入）：开了浏览器的智能体据此给不给 computer use
    supports_vision: bool = False

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> PlatformModel:
        return cls(
            name=str(raw["name"]),
            display_name=str(raw.get("display_name") or raw["name"]),
            provider=str(raw.get("provider") or ""),
            max_input_tokens=raw.get("max_input_tokens"),
            max_output_tokens=raw.get("max_output_tokens"),
            supports_vision=bool(raw.get("supports_vision", False)),
        )


@dataclass(frozen=True)
class ClientKey:
    """一把客户端 Key 此刻的样子。

    status  pending_models  刚创建、还没分配模型
            active          可用：key 是原文，models 是有效模型
            blocked         被管理员停用
            no_models       分配的模型都不可用了（被删、停用）
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


#: (租户, 客户端类型, 编码) → 上次取到的。平台暂时不可用时沿用。
#: ★ 键里必须有租户：模型和客户端都属于租户，不同租户可以起同样的客户端编码
_last: dict[tuple[UUID, str, str], ClientKey] = {}
#: 租户 → 上次取到的平台模型目录（模型属于租户，各看各的）
_last_catalog: dict[UUID, list[PlatformModel]] = {}


def reset_model_platform_cache() -> None:
    _last.clear()
    _last_catalog.clear()


async def client_key(
    settings: Settings, client_type: str, client_code: str, *, ensure_name: str | None = None
) -> ClientKey | None:
    """取最新的客户端 Key。ensure_name：平台上还没有就以这个名字补建（智能体用）。

    → None：平台上没有、也没让补建。
    平台不可用时沿用上次取到的，从没取到过抛 ModelPlatformUnavailable。
    """
    slot = (TenantScope.require(), client_type, client_code)
    try:
        client = ModelPlatformClient(settings)
        found = await client.get(client_type, client_code)
        if found is None and ensure_name is not None:
            found = await client.ensure(client_type, client_code, ensure_name)
    except ModelPlatformUnavailable:
        cached = _last.get(slot)
        if cached is None:
            raise
        logger.warning("模型管理平台不可用，沿用上次取到的客户端 Key（%s:%s）", *slot[1:])
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
    try:
        await ModelPlatformClient(settings).ensure(AGENT_CLIENT, str(agent_id), name)
    except ModelPlatformUnavailable as exc:
        logger.warning(
            "建智能体 %s 时没能在模型管理平台创建客户端 Key（之后补建）：%s", agent_id, exc
        )


async def rename_agent(settings: Settings, agent_id: UUID | str, name: str) -> None:
    """智能体改名：同步客户端 Key 的名字（只影响平台页面上的展示，尽力而为）。"""
    try:
        await ModelPlatformClient(settings).rename(AGENT_CLIENT, str(agent_id), name)
    except ModelPlatformUnavailable as exc:
        logger.info("同步客户端 Key 的名字失败（不影响使用）：%s", exc)


async def platform_catalog(settings: Settings) -> list[PlatformModel]:
    """本租户在平台上已登记、可调用的全部模型（客户端 Key 还没分配时编辑器先列它们）。"""
    tenant = TenantScope.require()
    try:
        found = await ModelPlatformClient(settings).catalog()
    except ModelPlatformUnavailable:
        if tenant not in _last_catalog:
            raise
        logger.warning("模型管理平台不可用，沿用上次取到的模型列表")
        return _last_catalog[tenant]
    if len(_last_catalog) > 10_000:
        _last_catalog.clear()
    _last_catalog[tenant] = found
    return found


async def sees_images(settings: Settings, model: str) -> bool:
    """模型平台登记时标注了能看图。没标注的、平台从没取到过的，都按不能。"""
    if not model:
        return False
    try:
        catalog = await platform_catalog(settings)
    except ModelPlatformUnavailable:
        logger.warning("模型管理平台不可用，按模型 %s 不能看图处理", model)
        return False
    return any(m.name == model and m.supports_vision for m in catalog)


# ───────────────────────────── 端点与检查


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    api_key: str
    headers: Mapping[str, str] = field(default_factory=dict)


def key_endpoint(settings: Settings, key: str) -> ModelEndpoint:
    """模型网关 + 客户端 Key。"""
    return ModelEndpoint(settings.model_gateway_url, key)


#: 模型网关认的会话头（atlas_modelgateway codecs.SESSION_HEADER）
SESSION_HEADER = "X-Session-Id"


def for_session(endpoint: ModelEndpoint, session: UUID) -> ModelEndpoint:
    """告诉模型网关这次调用属于哪个会话：调用记录据此对得上会话；同一个会话按它粘在同一条线路上，
    提示词缓存接得上。不带的话网关只能按 Key 粘，调用记录也看不出是哪个会话的。"""
    return replace(endpoint, headers={**endpoint.headers, SESSION_HEADER: str(session)})


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
    """模型规格换成经模型网关的调用方式：一律 Anthropic 格式。思考、力度、温度按意图原样发，
    按上游模型整理参数是网关的事（agent-platform/doc/design/model-gateway-design.html §4）。"""
    return replace(model, provider="anthropic")
