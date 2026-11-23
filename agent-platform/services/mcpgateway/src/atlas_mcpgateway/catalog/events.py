"""扩展点 CatalogEvents：目录变化时要别的领域跟着做的事。

服务目录定义、授权实现（access/handler.py 的 CatalogEventHandler）：登记服务时在用户中心登记
McpServer、登记人成为所有者；改显示名同步改名；工具删除时删掉 McpTool 对象和它的授权。

★ 同一个事务里同步分发：处理失败整个操作回滚——用户中心登记失败，服务就登记不上，不会出现没人能管
  的服务。不引入消息队列。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..db.models import McpService

__all__ = ["CatalogEvents"]


class CatalogEvents(Protocol):
    async def service_registered(self, service: McpService, owner: str) -> None: ...

    async def service_renamed(self, service: McpService) -> None: ...

    async def tools_removed(self, service: McpService, names: Sequence[str]) -> None:
        """names：刚被删掉（REST、gRPC）或上游刚没了（MCP 代理）的工具。"""
        ...
