"""MCP 工具目录的形状（doc/mcp-design.html 第二版 §4.1）。

工具定义是 MCP 网关拉取、存库、检查和复核过的数据；Atlas 只读（gateway.GatewayCatalog），
装配时读缓存、不连网，真正建连推迟到第一次调用。一个慢的服务不会拖慢每一轮，一个宕机的服务
也不会让所有挂了它的 Agent 发不出消息。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from ...domain.mcp_naming import model_name
from ...errors import CapabilityUnavailable
from .config import McpServerConfig

__all__ = [
    "McpCatalog",
    "McpToolDef",
    "McpUnavailable",
    "ServerSnapshot",
    "describe_error",
    "root_causes",
]


class McpUnavailable(CapabilityUnavailable):
    """spec 引用的服务不在网关 / 已停用，或网关的元数据取不到（又没有缓存过）。"""


@dataclass(frozen=True)
class McpToolDef:
    """一个工具的定义（来自网关的元数据）。可 JSON 往返。

    ★ 与 domain.tool_registry.ToolDef（内置工具）不是一回事，故意不同名。
    """

    server: str
    name: str  # 线上名
    description: str
    input_schema: dict[str, Any]
    #: MCP 线上格式的注解（readOnlyHint …）
    annotations: dict[str, Any] | None = None
    #: 定义指纹（网关算的）：名字、描述、参数定义、只读注解
    digest: str = ""
    #: 机械检查不通过的原因（网关给出）。非空即不可选（§4.2）
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
            input_schema=self.input_schema,
            annotations=ToolAnnotations.model_validate(self.annotations)
            if self.annotations
            else None,
        )


@dataclass(frozen=True)
class ServerSnapshot:
    server: str
    tools: tuple[McpToolDef, ...]
    #: 全部 digest 的哈希 —— 任何一个工具的任何字段变了，它就变
    content_hash: str
    fetched_at: float
    #: 网关最近一次拉取失败的原因；tools 仍是上次成功的那份
    error: str | None = None
    #: content_hash 最近一次变化的时间
    changed_at: float | None = None
    #: 每个工具变化前那一版（没变的就是它自己）—— 管理页据此给出「描述改了什么」
    previous: tuple[McpToolDef, ...] = ()
    #: 上游已经删掉的工具
    removed: tuple[str, ...] = ()

    def tool(self, name: str) -> McpToolDef | None:
        return next((t for t in self.tools if t.name == name), None)


class McpCatalog(Protocol):
    """MCP 工具目录（gateway.GatewayCatalog；测试里用内存替身）。"""

    def servers(self) -> list[McpServerConfig]: ...

    def server(self, name: str) -> McpServerConfig | None: ...

    async def snapshot(self, name: str) -> ServerSnapshot: ...

    async def refresh(self, name: str) -> ServerSnapshot: ...


def root_causes(exc: BaseException) -> list[BaseException]:
    """真正的原因：ExceptionGroup 展开到叶子（anyio TaskGroup 会包一层），再顺着
    显式的 __cause__ 往下走（FastMCP 把连接失败包成 RuntimeError … from ConnectError）。

    ★ cause 是 CancelledError 时停下：asyncio.timeout 抛的 TimeoutError 就是
      from CancelledError —— 取消是超时的实现方式，不是原因。
    """
    if isinstance(exc, BaseExceptionGroup):
        return [root for sub in exc.exceptions for root in root_causes(sub)]
    cause = exc.__cause__
    if cause is not None and not isinstance(cause, asyncio.CancelledError):
        return root_causes(cause)
    return [exc]


def describe_error(exc: BaseException) -> str:
    root = root_causes(exc)[0]
    return f"{type(root).__name__}: {root}"[:500]
