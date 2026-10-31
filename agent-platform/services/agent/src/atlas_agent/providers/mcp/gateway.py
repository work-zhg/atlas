"""MCP 网关在 Atlas 这一侧：元数据、智能体的 Key 能用什么、目录。

    元数据       服务、工具定义都从网关读（网关是事实源，Atlas 不自己发现工具）。
                 进程内短时缓存 + Redis 里留一份：网关暂时不可用时，装配照样能从缓存造出工具
    智能体 Key   每个智能体一把（客户端类型 Agent、编码 = 智能体 id）：建智能体时申请，
                 网关接入前建的智能体在编辑器、Run 开始时补申请。网关给出这把 Key 此刻能用的
                 服务和工具（公开 + 授给它的），和数据面判权是同一份。
                 ★ 与智能体的权限类型无关：同一个智能体对所有使用者一样（同模型管理平台）
                 ★ Key 不进 Redis、不进日志：只活在内存和 MCP 客户端的传输层里
    GatewayCatalog  实现 McpCatalog：服务地址都是网关上的 /mcp/{服务名}

★ 网关没有复核了：元数据里 review_required 恒为 False，不能用的工具（定义不合规、管理员停用）
  带 issues，Atlas 沿用原来的闸门代码。
★ 网关暂时不可用：沿用这个进程上次取到的 Key 和能用的工具（最终由网关判权）；从没取到过的才报
  暂时不可用。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from ...errors import CapabilityUnavailable
from ...outbound import outbound_headers
from .catalog import McpToolDef, McpUnavailable, ServerSnapshot
from .config import McpServerConfig
from .review import ReviewIndex

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ...config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_CLIENT",
    "AgentAccess",
    "GatewayCatalog",
    "GatewayClient",
    "GatewayUnavailable",
    "agent_access",
    "delete_agent_key",
    "forget_access",
    "gateway_catalog",
    "make_mcp_catalog",
    "mcp_agent_access",
    "register_agent_key",
    "rename_agent_key",
    "reset_gateway_cache",
    "servers_in",
]

#: 智能体的客户端类型（网关的 ATLAS_MCPGW_CLIENT_TYPES 里登记给应用 atlas）
AGENT_CLIENT = "Agent"

_REDIS_KEY = "atlas:mcp:gateway:metadata"
#: Redis 里那份的保留时间：网关长时间不可用时，过了这么久就不再用旧定义
_REDIS_TTL_S = 7 * 24 * 3600


class GatewayUnavailable(CapabilityUnavailable):
    """连不上 MCP 网关，或网关回了错误。"""


@dataclass(frozen=True)
class AgentAccess:
    """一个智能体的 MCP Key 此刻能用什么（网关算好的：公开服务 + 授给这把 Key 的）。"""

    #: Key 在用户中心的主体（事件、日志里标出是谁）
    subject: str
    #: 服务 → 能用的工具（已去掉停用、不合规的）
    servers: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: 原文；停用时为 None
    key: str | None = None
    #: active | blocked
    key_status: str = "active"

    def allows(self, service: str) -> bool:
        return service in self.servers

    def tools_of(self, service: str) -> frozenset[str]:
        return self.servers.get(service, frozenset())

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> AgentAccess:
        return cls(
            subject=str(body["key_uid"]),
            servers={
                str(s["name"]): frozenset(s.get("tools") or ()) for s in body.get("servers") or ()
            },
            key=body.get("key"),
            key_status=str(body.get("status") or "active"),
        )


class GatewayClient:
    """网关的内部接口（/internal/*）。内网免令牌，身份请求头随出站身份透传。"""

    #: 测试替换用（生产为 None，走真实网络）
    transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, base_url: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    def service_url(self, name: str) -> str:
        """Atlas 和 Pod 里的 CLI 连的地址：网关上的服务。"""
        return f"{self.base_url}/mcp/{name}"

    async def _request(self, method: str, path: str, *, missing_ok: bool = False, **kw: Any) -> Any:
        """→ 响应体。missing_ok：404 返回 None（客户端 Key 还没有）。"""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=GatewayClient.transport
            ) as http:
                r = await http.request(
                    method, f"{self.base_url}{path}", headers=outbound_headers(), **kw
                )
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"MCP 网关暂时不可用：{type(exc).__name__}") from exc
        if r.status_code == 404 and missing_ok:
            return None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise GatewayUnavailable(f"MCP 网关返回 HTTP {r.status_code}：{detail}")
        return r.json()

    async def metadata(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/internal/metadata"))["services"])

    async def client_key(self, code: str) -> AgentAccess | None:
        body = await self._request(
            "GET",
            "/internal/client-keys",
            params={"client_type": AGENT_CLIENT, "client_code": code},
            missing_ok=True,
        )
        return AgentAccess.parse(body) if body is not None else None

    async def ensure_client_key(self, code: str, name: str, owner: UUID | str) -> AgentAccess:
        body = {
            "client_type": AGENT_CLIENT,
            "client_code": code,
            "name": name[:200],
            "owner_uid": str(owner),
        }
        return AgentAccess.parse(await self._request("POST", "/internal/client-keys", json=body))

    async def rename_client_key(self, code: str, name: str) -> None:
        body = {"client_type": AGENT_CLIENT, "client_code": code, "name": name[:200]}
        await self._request("PATCH", "/internal/client-keys", json=body)

    async def delete_client_key(self, code: str) -> None:
        await self._request(
            "DELETE",
            "/internal/client-keys",
            params={"client_type": AGENT_CLIENT, "client_code": code},
        )


# ───────────────────────────── 元数据 → Atlas 的目录


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def _tool(server: str, raw: Mapping[str, Any]) -> McpToolDef:
    return McpToolDef(
        server=server,
        name=str(raw["name"]),
        description=str(raw.get("description") or ""),
        input_schema=dict(raw.get("input_schema") or {}),
        annotations=dict(raw["annotations"]) if raw.get("annotations") else None,
        digest=str(raw["digest"]),
        issues=tuple(raw.get("issues") or ()),
    )


def snapshot_of(meta: Mapping[str, Any]) -> ServerSnapshot:
    """网关的一个服务 → ServerSnapshot（装配、编辑器读它，不连网）。

    ★ previous 给全量的「上一版」：没变的工具上一版就是它自己，变了的用网关留下的那一版 ——
      和本地发现时 ServerSnapshot.previous 的含义一致（整份列表的上一版）。
    """
    server = str(meta["name"])
    tools = tuple(_tool(server, t) for t in meta.get("tools") or ())
    previous = []
    for raw, current in zip(meta.get("tools") or (), tools, strict=True):
        before = raw.get("previous")
        previous.append(_tool(server, {"name": current.name, **before}) if before else current)
    return ServerSnapshot(
        server=server,
        tools=tools,
        content_hash=str(meta.get("content_hash") or ""),
        fetched_at=_ts(meta.get("fetched_at")) or 0.0,
        error=meta.get("fetch_error"),
        changed_at=_ts(meta.get("changed_at")),
        previous=tuple(previous),
        removed=tuple(meta.get("removed_tools") or ()),
    )


@dataclass
class GatewayCatalog:
    """McpCatalog 的网关实现：定义来自网关的元数据，装配时只读缓存、不连网。"""

    client: GatewayClient
    metadata: list[dict[str, Any]]
    _by_name: dict[str, dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self._by_name = {str(m["name"]): m for m in self.metadata}

    def _config(self, meta: Mapping[str, Any]) -> McpServerConfig:
        return McpServerConfig(
            name=str(meta["name"]),
            url=self.client.service_url(str(meta["name"])),
            enabled=bool(meta.get("enabled")),
            call_timeout_s=meta.get("call_timeout_s"),
            review_required=bool(meta.get("review_required")),
        )

    def servers(self) -> list[McpServerConfig]:
        return [self._config(m) for m in self.metadata if m.get("enabled")]

    def all_servers(self) -> list[McpServerConfig]:
        """含停用的（管理页列表要看见它们）。"""
        return [self._config(m) for m in self.metadata]

    def server(self, name: str) -> McpServerConfig | None:
        meta = self._by_name.get(name)
        return self._config(meta) if meta is not None and meta.get("enabled") else None

    async def snapshot(self, name: str) -> ServerSnapshot:
        meta = self._by_name.get(name)
        if meta is None or not meta.get("enabled"):
            raise McpUnavailable(f"MCP 服务 {name!r} 未在 MCP 网关注册或已停用")
        return snapshot_of(meta)

    async def cached(self, name: str) -> ServerSnapshot | None:
        meta = self._by_name.get(name)
        return snapshot_of(meta) if meta is not None else None

    def public_access(self) -> dict[str, frozenset[str]]:
        """公开服务 → 能用的工具（任何有效的 Key 都能用）。新建的智能体还没有 Key：只能选这些。"""
        return {
            str(m["name"]): frozenset(
                str(t["name"]) for t in m.get("tools") or () if not t.get("issues")
            )
            for m in self.metadata
            if m.get("enabled") and m.get("visibility") == "public"
        }

    def reviews(self) -> ReviewIndex:
        """网关算好的复核状态 → ReviewIndex（review.review_status 据此得出同样的结论）。"""
        out: dict[str, dict[tuple[str, str], str]] = {}
        for meta in self.metadata:
            decisions: dict[tuple[str, str], str] = {}
            for t in meta.get("tools") or ():
                if t.get("status") == "rejected":
                    decisions[(str(t["name"]), str(t["digest"]))] = "rejected"
                elif t.get("status") == "ok" and meta.get("review_required"):
                    decisions[(str(t["name"]), str(t["digest"]))] = "approved"
            out[str(meta["name"])] = decisions
        return out


# ───────────────────────────── 缓存（进程内）


class _MetadataCache:
    """元数据：TTL 内直接用；过期了问网关，问不到就继续用上一份（进程内或 Redis 里的）。

    ★ 与技能目录同一条规则：一个依赖暂时不可用，不该让所有挂了 MCP 的 Agent
      发不出消息 —— 调用时自会报错（§4.1）。
    """

    def __init__(self) -> None:
        self.value: list[dict[str, Any]] | None = None
        self.at = 0.0

    def forget(self) -> None:
        self.at = 0.0

    async def get(
        self, client: GatewayClient, ttl_s: float, redis: aioredis.Redis | None
    ) -> list[dict[str, Any]]:
        if self.value is not None and time.monotonic() - self.at < ttl_s:
            return self.value
        try:
            self.value = await client.metadata()
            await _write_copy(redis, self.value)
        except GatewayUnavailable:
            if self.value is None:
                self.value = await _read_copy(redis)
            if self.value is None:
                raise
            logger.warning("MCP 网关不可用，继续使用缓存的元数据")
        self.at = time.monotonic()
        return self.value


async def _write_copy(redis: aioredis.Redis | None, value: list[dict[str, Any]]) -> None:
    if redis is None:
        return
    try:
        await redis.set(_REDIS_KEY, json.dumps(value, ensure_ascii=False), ex=_REDIS_TTL_S)
    except Exception:
        logger.warning("写入 MCP 元数据缓存失败", exc_info=True)


async def _read_copy(redis: aioredis.Redis | None) -> list[dict[str, Any]] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_REDIS_KEY)
    except Exception:
        logger.warning("读取 MCP 元数据缓存失败", exc_info=True)
        return None
    return list(json.loads(raw)) if raw else None


_metadata = _MetadataCache()
#: 智能体 id → (过期时刻, 能用什么)
_access: dict[str, tuple[float, AgentAccess]] = {}
#: 智能体 id → 上次取到的（网关暂时不可用时沿用）
_last: dict[str, AgentAccess] = {}


def reset_gateway_cache() -> None:
    _metadata.value, _metadata.at = None, 0.0
    _access.clear()
    _last.clear()


async def gateway_catalog(
    base_url: str, *, ttl_s: float, redis: aioredis.Redis | None
) -> GatewayCatalog:
    client = GatewayClient(base_url)
    return GatewayCatalog(client, await _metadata.get(client, ttl_s, redis))


async def agent_access(
    base_url: str, agent_id: UUID | str, *, name: str, owner: UUID | str, ttl_s: float
) -> AgentAccess:
    """智能体的 Key 此刻能用什么。网关上还没有这把 Key 就补申请（owner = 主体的负责人）。

    网关不可用时沿用上次取到的；从没取到过抛 GatewayUnavailable（调用方决定跳过还是失败）。
    """
    code = str(agent_id)
    row = _access.get(code)
    if row is not None and row[0] >= time.monotonic():
        return row[1]
    client = GatewayClient(base_url)
    try:
        found = await client.client_key(code)
        if found is None:
            found = await client.ensure_client_key(code, name, owner)
    except GatewayUnavailable:
        cached = _last.get(code)
        if cached is None:
            raise
        logger.warning("MCP 网关不可用，沿用上次取到的智能体 Key（%s）", code)
        return cached
    if len(_access) > 10_000:
        _access.clear()
        _last.clear()
    _last[code] = found
    if ttl_s > 0:
        _access[code] = (time.monotonic() + ttl_s, found)
    return found


def _gateway_url(settings: Settings) -> str:
    if not settings.mcp_gateway_url:
        raise GatewayUnavailable("没有配置 MCP 网关（MCP_GATEWAY_URL），用不了 MCP 工具")
    return settings.mcp_gateway_url


async def make_mcp_catalog(settings: Settings, redis: aioredis.Redis | None) -> GatewayCatalog:
    """MCP 工具目录：服务、工具定义都来自 MCP 网关（读缓存）。"""
    return await gateway_catalog(
        _gateway_url(settings), ttl_s=settings.mcp_metadata_ttl_s, redis=redis
    )


async def mcp_agent_access(
    settings: Settings, agent_id: UUID | str, *, name: str, owner: UUID | str
) -> AgentAccess:
    """这个智能体的 Key 能用哪些服务和工具、Key 是什么。网关不可用抛 GatewayUnavailable。"""
    return await agent_access(
        _gateway_url(settings), agent_id, name=name, owner=owner, ttl_s=settings.mcp_access_ttl_s
    )


def forget_access(agent_id: UUID | str) -> None:
    """Key 刚被网关拒绝（可能刚重置）、授权刚改过：下次重新取。"""
    _access.pop(str(agent_id), None)


async def register_agent_key(
    settings: Settings, agent_id: UUID | str, name: str, owner: UUID | str
) -> None:
    """建智能体时申请 Key。★ 尽力而为：网关暂时不可用不挡住建智能体，编辑器、Run 开始时补申请。"""
    if not settings.mcp_gateway_url:
        return
    try:
        await GatewayClient(settings.mcp_gateway_url).ensure_client_key(str(agent_id), name, owner)
    except GatewayUnavailable as exc:
        logger.warning("建智能体 %s 时没能在 MCP 网关申请 Key（之后补申请）：%s", agent_id, exc)


async def rename_agent_key(settings: Settings, agent_id: UUID | str, name: str) -> None:
    """智能体改名：同步 Key 的名字（只影响网关页面上的展示，尽力而为）。"""
    if not settings.mcp_gateway_url:
        return
    try:
        await GatewayClient(settings.mcp_gateway_url).rename_client_key(str(agent_id), name)
    except GatewayUnavailable as exc:
        logger.info("同步智能体 Key 的名字失败（不影响使用）：%s", exc)


async def delete_agent_key(settings: Settings, agent_id: UUID | str) -> None:
    """删除（归档）智能体：删掉它的 Key（网关同时停用用户中心里的主体）。尽力而为。"""
    if not settings.mcp_gateway_url:
        return
    forget_access(agent_id)
    _last.pop(str(agent_id), None)
    try:
        await GatewayClient(settings.mcp_gateway_url).delete_client_key(str(agent_id))
    except GatewayUnavailable as exc:
        logger.warning(
            "删除智能体 %s 的 MCP Key 失败（请在 MCP 网关页面上删除）：%s", agent_id, exc
        )


def servers_in(tool_names: Iterable[str]) -> set[str]:
    from ...domain.mcp_naming import parse_tool_id

    return {p[0] for name in tool_names if (p := parse_tool_id(name)) is not None}
