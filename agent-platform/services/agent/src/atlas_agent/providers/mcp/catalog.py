"""MCP 工具目录：发现与调用分离（MCP 详设 §05）。

此前每个 run 装配都对引用到的 server 做一次 tools/list，/v1/tools 每次请求
连**全部** server —— 一个慢 server 拖慢每一轮与编辑器，一个宕机的 server
让所有勾了它的 agent 发不出消息。

现在工具定义是**缓存出来的数据**：
  · 装配期只读缓存，命中时零网络。adapter 能凭定义 + 连接参数构造出工具，
    建连推迟到 tools/call（native.py）。
  · 软过期（默认 10 min）后抢到锁的那一个同步刷新，其余继续用旧值；
    刷新失败**保留旧定义**并记下原因 —— server 短暂宕机不该让已缓存的
    工具消失，调用期自会报错。
  · 硬过期（7 天）或从未发现过 = 冷 miss，同步拉一次；失败即 McpUnavailable。

★ 软过期刻意做成同步而不是后台任务：Redis 客户端的生命周期属于调用方
  （请求 / run），后台任务会拿着一个随时被关掉的连接。代价是每 10 分钟
  有一个调用方多等一次 tools/list，可以接受。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

import redis.asyncio as aioredis

from ...domain.mcp_naming import model_name
from ...errors import CapabilityUnavailable
from .config import McpServerConfig, connection_for

logger = logging.getLogger(__name__)

_KEY = "atlas:mcp:snap:{}"
_LOCK = "atlas:mcp:lock:{}"

#: 描述超长本身就是可疑信号（MCP 概设 §08）
MAX_DESCRIPTION_CHARS = 2000


class McpUnavailable(CapabilityUnavailable):
    """spec 引用的 server 不在目录 / 已禁用，或冷缓存且连不上。"""


@dataclass(frozen=True)
class McpToolDef:
    """一次 tools/list 里的一个工具，已规范化。可 JSON 往返。

    ★ 与 domain.tool_registry.ToolDef（内置工具）不是一回事，故意不同名。
    """

    server: str
    name: str  # 线上名
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None = None
    #: sha256(name, description, input_schema)。本期不据此做判断 —— 留给配置平面
    #: 的「定义变更 → 待审」用（MCP 详设 §09）。
    digest: str = ""
    #: 不可用的原因（命名非法 / 描述超长 / schema 形状不对）。非空即不可选 ——
    #: 宁可让管理员来确认，也不让可疑定义悄悄进入模型上下文（§10）。
    issues: tuple[str, ...] = ()

    @property
    def model_name(self) -> str | None:
        return model_name(self.server, self.name)

    @property
    def usable(self) -> bool:
        return not self.issues

    def to_mcp_tool(self) -> Any:
        from mcp.types import Tool, ToolAnnotations

        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
            annotations=ToolAnnotations(**self.annotations) if self.annotations else None,
        )


@dataclass(frozen=True)
class ServerSnapshot:
    server: str
    tools: tuple[McpToolDef, ...]
    #: 全部 digest 的哈希 —— 任何一个工具的任何字段变了，它就变
    content_hash: str
    fetched_at: float
    #: 最近一次刷新失败的原因；tools 仍是上次成功的那份
    error: str | None = None
    #: content_hash 最近一次变化的时间（技能 / MCP 设计 §8.2）
    changed_at: float | None = None
    #: 变化前那一版的工具定义（只留一版）—— 管理页据此给出「描述改了什么」
    previous: tuple[McpToolDef, ...] = ()

    def tool(self, name: str) -> McpToolDef | None:
        return next((t for t in self.tools if t.name == name), None)

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> ServerSnapshot:
        data = json.loads(raw)

        def defs(items: list[dict[str, Any]]) -> tuple[McpToolDef, ...]:
            return tuple(McpToolDef(**{**t, "issues": tuple(t.get("issues") or ())}) for t in items)

        return cls(
            **{**data, "tools": defs(data["tools"]), "previous": defs(data.get("previous") or [])}
        )


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalize(server: str, tool: Any) -> McpToolDef:
    """mcp.types.Tool → McpToolDef，顺带做发现期检查。"""
    description = tool.description or ""
    schema = dict(tool.inputSchema or {})
    issues: list[str] = []
    if model_name(server, tool.name) is None:
        issues.append(f"工具名 {tool.name!r} 不符合模型侧命名规则（[A-Za-z0-9_-]，总长 ≤64）")
    if len(description) > MAX_DESCRIPTION_CHARS:
        issues.append(f"描述过长（{len(description)} > {MAX_DESCRIPTION_CHARS} 字符）")
    if schema.get("type") != "object":
        issues.append("inputSchema.type 不是 object")
    annotations = tool.annotations.model_dump(exclude_none=True) if tool.annotations else None
    return McpToolDef(
        server=server,
        name=tool.name,
        description=description,
        input_schema=schema,
        annotations=annotations or None,
        digest=_sha(_canonical([tool.name, description, schema])),
        issues=tuple(issues),
    )


def build_snapshot(server: str, tools: list[McpToolDef]) -> ServerSnapshot:
    ordered = tuple(sorted(tools, key=lambda t: t.name))
    return ServerSnapshot(
        server=server,
        tools=ordered,
        content_hash=_sha(_canonical([t.digest for t in ordered])),
        fetched_at=time.time(),
    )


async def discover(server: McpServerConfig, *, timeout_s: float) -> ServerSnapshot:
    """对一个 server 做一次完整的 tools/list（含分页）。"""
    from langchain_mcp_adapters.sessions import create_session
    from mcp.types import PaginatedRequestParams

    async with asyncio.timeout(timeout_s):
        async with create_session(connection_for(server, timeout_s=timeout_s)) as session:  # type: ignore[arg-type]
            await session.initialize()
            found: list[Any] = []
            cursor: str | None = None
            for _ in range(100):  # 防一个永远返回 nextCursor 的 server
                page = await session.list_tools(
                    params=PaginatedRequestParams(cursor=cursor) if cursor else None
                )
                found.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
    return build_snapshot(server.name, [normalize(server.name, t) for t in found])


def describe_error(exc: BaseException) -> str:
    """ExceptionGroup 里真正的原因往往在叶子上（anyio TaskGroup 会包一层）。"""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"[:500]


def _carry_history(fresh: ServerSnapshot, cached: ServerSnapshot | None) -> ServerSnapshot:
    """新快照接上旧快照的变更记录；定义变了就把旧定义留作 previous。"""
    if cached is None:
        return fresh
    if cached.content_hash != fresh.content_hash:
        logger.warning(
            "MCP server %s 的工具定义变了：%s → %s",
            fresh.server,
            cached.content_hash[:12],
            fresh.content_hash[:12],
        )
        return replace(fresh, changed_at=fresh.fetched_at, previous=cached.tools)
    return replace(fresh, changed_at=cached.changed_at, previous=cached.previous)


class McpCatalog(Protocol):
    """MCP 工具目录。配置平面上线后换一个实现（ConfigPlaneCatalog），调用方不动。"""

    def servers(self) -> list[McpServerConfig]: ...

    def server(self, name: str) -> McpServerConfig | None: ...

    async def snapshot(self, name: str) -> ServerSnapshot: ...

    async def refresh(self, name: str) -> ServerSnapshot: ...


@dataclass
class SettingsCatalog:
    """server 定义来自 Settings.mcp_servers（.env），工具定义缓存在 Redis。

    ★ 不在 Python 侧建 mcp_server 表：MCP 注册表归配置平面（包结构 §02），
      建了就是注定要迁走的数据。过渡期改 MCP 配置仍需改 .env 并重启。
    """

    configs: list[McpServerConfig]
    redis: aioredis.Redis | None
    soft_ttl_s: float = 600
    hard_ttl_s: int = 7 * 24 * 3600
    discovery_timeout_s: float = 10
    _enabled: dict[str, McpServerConfig] = field(init=False)

    def __post_init__(self) -> None:
        self._enabled = {c.name: c for c in self.configs if c.enabled}

    def servers(self) -> list[McpServerConfig]:
        return list(self._enabled.values())

    def server(self, name: str) -> McpServerConfig | None:
        return self._enabled.get(name)

    async def snapshot(self, name: str) -> ServerSnapshot:
        config = self._require(name)
        cached = await self._read(name)
        if cached is None:
            # 冷 miss：没有定义就构造不出工具 schema，只能同步拉
            return await self.refresh(name)
        if time.time() - cached.fetched_at < self.soft_ttl_s:
            return cached
        if not await self._try_lock(name):
            return cached  # 别人在刷，先用旧的
        try:
            fresh = await discover(config, timeout_s=self.discovery_timeout_s)
        except Exception as exc:
            logger.warning("MCP server %s 刷新失败，继续使用缓存：%s", name, describe_error(exc))
            # ★ fetched_at 推到现在：下一次重试在一个软 TTL 之后。不推的话锁一过期
            #   （30s）就又有一个 run 要陪着宕机的 server 等满发现超时。
            #   tools 仍是旧的，error 写回让目录能看见。
            stale = replace(cached, error=describe_error(exc), fetched_at=time.time())
            await self._write(stale)
            return stale
        fresh = _carry_history(fresh, cached)
        await self._write(fresh)
        return fresh

    async def refresh(self, name: str) -> ServerSnapshot:
        config = self._require(name)
        try:
            fresh = await discover(config, timeout_s=self.discovery_timeout_s)
        except Exception as exc:
            raise McpUnavailable(
                f"MCP server {name!r} 不可用，且没有可用的工具定义缓存：{describe_error(exc)}"
            ) from exc
        fresh = _carry_history(fresh, await self._read(name))
        await self._write(fresh)
        return fresh

    async def cached(self, name: str) -> ServerSnapshot | None:
        """只读缓存，不触发任何发现 —— 管理页的列表不能被一个慢 server 拖住。"""
        self._require(name)
        return await self._read(name)

    # ------------------------------------------------------------------ 内部

    def _require(self, name: str) -> McpServerConfig:
        config = self._enabled.get(name)
        if config is None:
            raise McpUnavailable(f"MCP server {name!r} 未注册或已禁用")
        return config

    async def _read(self, name: str) -> ServerSnapshot | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(_KEY.format(name))
        except Exception:
            # Redis 挂了不该让 MCP 整体不可用 —— 退化为直连发现
            logger.warning("读取 MCP 目录缓存失败", exc_info=True)
            return None
        if not raw:
            return None
        try:
            return ServerSnapshot.from_json(raw)
        except Exception:
            logger.warning("MCP 目录缓存格式不对，丢弃：%s", name, exc_info=True)
            return None

    async def _write(self, snap: ServerSnapshot) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(_KEY.format(snap.server), snap.to_json(), ex=self.hard_ttl_s)
        except Exception:
            logger.warning("写入 MCP 目录缓存失败", exc_info=True)

    async def _try_lock(self, name: str) -> bool:
        if self.redis is None:
            return True
        try:
            lock_ttl = max(int(self.discovery_timeout_s) * 3, 5)
            return bool(await self.redis.set(_LOCK.format(name), "1", nx=True, ex=lock_ttl))
        except Exception:
            return True
