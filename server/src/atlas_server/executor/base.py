"""RunExecutor 协议（文档 §12.1）。

刻意做成 Protocol：MVP 用进程内 asyncio.Task，将来换成 arq / Celery worker 时
**只加一个实现类 + 改一处依赖注入**，API 层与 engine 层零改动。
触发迁移的信号：单 run 常态超 5 分钟 / 部署频繁到 interrupted 成为常见投诉 /
需要排队与优先级。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class RunExecutor(Protocol):
    async def submit(self, run_id: UUID) -> None:
        """排入执行。不阻塞调用方 —— HTTP 请求要立刻返回 run_id。"""
        ...

    async def cancel(self, run_id: UUID) -> None: ...

    async def shutdown(self) -> None:
        """优雅停机：给在跑的 run 一点时间收尾（§12.1 的 30s 窗口）。"""
        ...
