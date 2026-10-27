"""一个 Run 段的 MCP 状态（MCP 方案设计 §6.3、§6.7）：会话复用、主体 Key、「用不了」的记忆。

    McpSessions.hold     同一个 Run 段对同一个服务复用一个会话：第一次调用时建（握手 + SDK 补拉一次
                         工具列表），之后每次调用只发 1 个请求。N 次调用从 4N 个请求降到 N + 3
    McpSessions.aclose   Run 段结束（完成、挂起、取消、被接管）时关闭；恢复后重新建立，不跨进程迁移
    McpSessions.reset    会话失效（服务重启、会话过期）：关掉，下次调用重建（§6.7 ⑧）
    KeyAuth              主体 Key 放在传输层：模型看不到、改不了；每个请求现取，Key 被网关拒绝后
                         换成新取的值，已打开的会话不用重建（§6.7 ④）
    block / blocked      ⑤ ⑥ 和 ② 的结论在本 Run 段内记住：之后对同一个服务 / 工具的调用直接交回
                         同样的说明，不再发请求（§6.7）

★ FastMCP 的 client 可重入、按引用计数：这里持有一层，langchain.mcp 的工具每次调用时
  `async with client` 再进一层就复用同一个会话。持有在工具任务里进入、在 Run 段收尾时退出，
  FastMCP 的会话跑在它自己的后台任务里，不会有跨任务退出的问题（10-25 实测）。
★ 一个 Run 只有一个权限主体，所以会话天然按主体分开，不会跨主体复用。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Generator
from typing import Any

import httpx2

logger = logging.getLogger(__name__)

__all__ = ["KeyAuth", "McpSessions"]


class KeyAuth(httpx2.Auth):
    """Authorization: Bearer <主体 Key>，每个请求现取。"""

    def __init__(self, key: str) -> None:
        self.key = key

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, Any]:
        request.headers["Authorization"] = f"Bearer {self.key}"
        yield request


#: 重新取 Key：→ 新的原文；没有可用的 Key 为 None
KeyFetcher = Callable[[], Awaitable[str | None]]


class McpSessions:
    def __init__(self, *, auth: KeyAuth | None = None, fetch_key: KeyFetcher | None = None) -> None:
        self.auth = auth
        self._fetch_key = fetch_key
        self._clients: dict[str, Any] = {}
        self._held: set[str] = set()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        #: (服务, 工具) → 给模型的说明。工具为 None = 整个服务；服务也为 None = 本 Run 全部
        self._blocked: dict[tuple[str | None, str | None], str] = {}

    def register(self, server: str, client: Any) -> None:
        self._clients[server] = client

    async def hold(self, server: str) -> None:
        """第一次用到这个服务时建会话；同一步并发的调用按服务加锁，只建一次。"""
        if server in self._held:
            return
        async with self._locks[server]:
            if server in self._held:
                return
            await self._clients[server].__aenter__()
            self._held.add(server)

    async def reset(self, server: str) -> None:
        """会话失效：彻底关掉，下次 hold 重新握手。"""
        async with self._locks[server]:
            client = self._clients.get(server)
            self._held.discard(server)
            if client is not None:
                await client.close()

    async def refresh_key(self) -> bool:
        """Key 被网关拒绝（可能刚被重置）：重新取一次。→ 取到了新的值（可以重试一次）。"""
        if self.auth is None or self._fetch_key is None:
            return False
        fresh = await self._fetch_key()
        if fresh is None or fresh == self.auth.key:
            return False
        self.auth.key = fresh
        return True

    def block(self, server: str | None, tool: str | None, message: str) -> None:
        self._blocked[(server, tool)] = message

    def blocked(self, server: str, tool: str) -> str | None:
        return (
            self._blocked.get((None, None))
            or self._blocked.get((server, None))
            or self._blocked.get((server, tool))
        )

    async def aclose(self) -> None:
        """Run 段结束：关掉本段建过的全部会话。一个关不掉不影响别的。"""
        for server in sorted(self._held):
            try:
                await self._clients[server].close()
            except Exception:
                logger.warning("关闭 MCP 会话失败 server=%s", server, exc_info=True)
        self._held.clear()
