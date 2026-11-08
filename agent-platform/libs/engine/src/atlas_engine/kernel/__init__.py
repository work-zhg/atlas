"""kernel —— Atlas 的 agent 能力层：全部中间件和它们的排序规则。

它不发明执行循环 —— 循环来自 langchain.agents；kernel 提供的是一套可插拔的
能力层（文件系统 · 子智能体 · 技能 · 上下文管理 · 审批 · 限额），以中间件的形式叠加
到那个循环上。装哪些、什么顺序在 atlas_engine.runtime.assemble（装配的唯一住所），
顺序规则在 stack.py。

★ kernel 是**内部层，不是公共契约**：模块路径随时可能变。server 只认
  atlas_engine.contracts（类型与协议）和 atlas_engine.runtime（原生运行时的入口），
  import-linter 禁止 server 直接 import kernel。kernel 自己只许依赖 contracts
  （tests/test_engine_purity.py）。

★ 这里刻意**不导出 __version__**：版本号只有一个来源，engine/pyproject.toml。
"""

from atlas_engine.kernel.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
    FsToolName,
)
from atlas_engine.kernel.middleware.subagents import (
    SubAgent,
    SubAgentMiddleware,
)
from atlas_engine.kernel.stack import MiddlewareStack, Slot

__all__ = [
    "FilesystemMiddleware",
    "FilesystemPermission",
    "FsToolName",
    "MiddlewareStack",
    "Slot",
    "SubAgent",
    "SubAgentMiddleware",
]
