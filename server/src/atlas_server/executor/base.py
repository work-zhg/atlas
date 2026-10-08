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

    async def resume_if_ready(self, run_id: UUID) -> bool:
        """等的东西到了就把这个挂起的 run 续上。返回是否真的续了。

        ★ 为什么要进协议：**唤醒有两个来源**，都在执行器之外。
            · 子 run 跑完   → 执行器自己的收尾路径调它
            · 审批被决策   → HTTP 端点调它（api/v1/runs.py::decide_approval）

          少了 HTTP 那一路，一个等审批的 run 要等到周期扫描（默认 60s）才动 ——
          用户点了「允许」之后界面要干等一分钟，看着像没生效。
        """
        ...

    async def shutdown(self) -> None:
        """优雅停机：给在跑的 run 一点时间收尾（§12.1 的 30s 窗口）。"""
        ...
