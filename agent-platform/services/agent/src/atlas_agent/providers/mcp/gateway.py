"""MCP 网关（doc/mcp-design.html 第二版）在 Atlas 这一侧：元数据、主体能用什么、目录。

    元数据       服务、工具定义、复核结论都从网关读（网关是事实源，Atlas 不再自己发现工具）。
                 进程内短时缓存 + Redis 里留一份：网关暂时不可用时，装配照样能从缓存造出工具
    主体能用什么  主体此刻能用哪些服务 + 主体的 API Key，一次问网关（demo F5）。进程内短时缓存，
                 ★ 不进 Redis、不进日志：Key 只活在内存和 MCP 客户端的传输层里（§6.2）
    GatewayCatalog  实现 McpCatalog：服务地址都是网关上的 /mcp/{服务名}，看不到 Higress

★ 复核状态由网关算好给出（ok / invalid / pending_review / rejected），这里换算成 ReviewIndex，
  装配、编辑器、ACP 下发沿用原来的闸门代码。
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
    "GatewayCatalog",
    "GatewayClient",
    "GatewayUnavailable",
    "SubjectAccess",
    "forget_access",
    "gateway_catalog",
    "make_mcp_catalog",
    "mcp_subject_access",
    "reset_gateway_cache",
    "servers_in",
    "subject_access",
]

_REDIS_KEY = "atlas:mcp:gateway:metadata"
#: Redis 里那份的保留时间：网关长时间不可用时，过了这么久就不再用旧定义
_REDIS_TTL_S = 7 * 24 * 3600


class GatewayUnavailable(CapabilityUnavailable):
    """连不上 MCP 网关，或网关回了错误。"""


@dataclass(frozen=True)
class SubjectAccess:
    """一个主体此刻能用什么（§5.4 闸门一）。"""

    subject: str
    #: 能用（≥ 使用）且已启用的服务
    services: frozenset[str]
    all_services: bool = False
    #: 原文；没有或已停用为 None
    key: str | None = None
    #: active | disabled | none
    key_status: str = "none"

    def allows(self, service: str) -> bool:
        return service in self.services


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

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=GatewayClient.transport
            ) as http:
                r = await http.request(
                    method, f"{self.base_url}{path}", headers=outbound_headers(), **kw
                )
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"MCP 网关暂时不可用：{type(exc).__name__}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise GatewayUnavailable(f"MCP 网关返回 HTTP {r.status_code}：{detail}")
        return r.json()

    async def metadata(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/internal/metadata"))["services"])

    async def access(self, subject: str) -> SubjectAccess:
        body = await self._request("GET", f"/internal/subjects/{subject}/access")
        return SubjectAccess(
            subject=str(body["subject"]),
            services=frozenset(body.get("services") or []),
            all_services=bool(body.get("all_services")),
            key=body.get("key"),
            key_status=str(body.get("key_status") or "none"),
        )

    async def refresh(self, name: str) -> dict[str, Any]:
        return dict(await self._request("POST", f"/internal/services/{name}/refresh"))


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

    async def refresh(self, name: str) -> ServerSnapshot:
        """让网关立即重新拉取（Atlas 的 MCP 页面「刷新」），并更新本进程的缓存。"""
        meta = await self.client.refresh(name)
        self._by_name[name] = meta
        self.metadata = [meta if str(m["name"]) == name else m for m in self.metadata]
        _metadata.forget()
        return snapshot_of(meta)

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

    ★ 与技能目录、配置服务注册表同一条规则：一个依赖暂时不可用，不该让所有挂了 MCP 的 Agent
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
#: 主体 → (过期时刻, 能用什么)
_access: dict[str, tuple[float, SubjectAccess]] = {}


def reset_gateway_cache() -> None:
    _metadata.value, _metadata.at = None, 0.0
    _access.clear()


async def gateway_catalog(
    base_url: str, *, ttl_s: float, redis: aioredis.Redis | None
) -> GatewayCatalog:
    client = GatewayClient(base_url)
    return GatewayCatalog(client, await _metadata.get(client, ttl_s, redis))


async def subject_access(base_url: str, subject: UUID | str, *, ttl_s: float) -> SubjectAccess:
    """主体此刻能用什么。网关不可用时抛 GatewayUnavailable（调用方决定跳过还是失败）。"""
    key = str(subject)
    row = _access.get(key)
    if row is not None and row[0] >= time.monotonic():
        return row[1]
    found = await GatewayClient(base_url).access(key)
    if ttl_s > 0:
        if len(_access) > 10_000:
            _access.clear()
        _access[key] = (time.monotonic() + ttl_s, found)
    return found


def _gateway_url(settings: Settings) -> str:
    if not settings.mcp_gateway_url:
        raise GatewayUnavailable("没有配置 MCP 网关（MCP_GATEWAY_URL），用不了 MCP 工具")
    return settings.mcp_gateway_url


async def make_mcp_catalog(settings: Settings, redis: aioredis.Redis | None) -> GatewayCatalog:
    """MCP 工具目录：服务、工具定义、复核结论都来自 MCP 网关（读缓存，§4.1）。"""
    return await gateway_catalog(
        _gateway_url(settings), ttl_s=settings.mcp_metadata_ttl_s, redis=redis
    )


async def mcp_subject_access(settings: Settings, subject: UUID) -> SubjectAccess:
    """这一轮的权限主体能用哪些服务、Key 是什么。网关不可用抛 GatewayUnavailable
    （调用方决定跳过还是失败）。"""
    return await subject_access(_gateway_url(settings), subject, ttl_s=settings.mcp_access_ttl_s)


def forget_access(subject: UUID | str) -> None:
    """Key 刚被网关拒绝（可能刚重置）：下次重新取（§6.7 ④）。"""
    _access.pop(str(subject), None)


def servers_in(tool_names: Iterable[str]) -> set[str]:
    from ...domain.mcp_naming import parse_tool_id

    return {p[0] for name in tool_names if (p := parse_tool_id(name)) is not None}
