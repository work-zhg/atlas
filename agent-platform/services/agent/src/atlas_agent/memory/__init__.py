"""跨会话记忆的 Atlas 一侧（doc/memory-design.html §4–§7）。

存储在记忆服务（atlas-memory）：它只按作用域实例隔离、按主题组织条目，负责并发、事务和
修订历史。存什么、怎么取、冲突怎么判断都在这里：

    client     记忆服务的 HTTP 客户端
    runtime    登记实例归属、常驻块与快照、版本表、load_memory / save_memory / forget_memory
    backfill   会话后补记（补漏 + 拆分超限的主题）
    render     给模型看的文本
    prompts    补记用的提示词

★ 记忆绝不能成为会话的硬依赖：记忆服务或用户中心不可用时，本会话暂时不用记忆，对话照常。
★ 关联主键由 Atlas 按会话确定（会话所属用户），Agent 和抽取模型都没法指定（§4.8）。
"""

from __future__ import annotations

from .backfill import Backfiller
from .client import (
    MemoryClient,
    MemoryEntry,
    MemoryRejected,
    MemoryTopic,
    MemoryUnavailable,
    ScopeRef,
    make_memory_client,
)
from .runtime import (
    FORGET_MEMORY_TOOL,
    LOAD_MEMORY_TOOL,
    SAVE_MEMORY_TOOL,
    SOURCE_AGENT,
    SOURCE_BACKFILL,
    SOURCE_USER,
    MemoryRuntime,
    MemoryTurn,
)

__all__ = [
    "FORGET_MEMORY_TOOL",
    "LOAD_MEMORY_TOOL",
    "SAVE_MEMORY_TOOL",
    "SOURCE_AGENT",
    "SOURCE_BACKFILL",
    "SOURCE_USER",
    "Backfiller",
    "MemoryClient",
    "MemoryEntry",
    "MemoryRejected",
    "MemoryRuntime",
    "MemoryTopic",
    "MemoryTurn",
    "MemoryUnavailable",
    "ScopeRef",
    "make_memory_client",
    "make_memory_runtime",
]


def make_memory_runtime(settings: object, sessionmaker: object) -> MemoryRuntime | None:
    """没接入记忆服务（memory_base_url 为空）就返回 None：调用方据此完全跳过记忆。"""
    client = make_memory_client(settings)
    if client is None:
        return None
    return MemoryRuntime(settings, sessionmaker, client)  # type: ignore[arg-type]
