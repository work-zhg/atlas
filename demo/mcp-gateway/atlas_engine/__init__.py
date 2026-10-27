"""atlas-engine：运行 Agent —— 建模型、组装图、驱动"模型 ↔ 工具"循环。

依赖方向：server → engine → langchain / langgraph。server 只认这里导出的东西：
    ToolSpec / ToolResult   中立的工具契约（contracts.py，不含任何 LangChain 类型）
    ModelSpec               用哪个模型（engine 自己建模型客户端）
    run_turn → TurnResult   跑一轮，交回中立的结果

★ 不认识 MCP：不 import mcp / fastmcp / httpx。工具从哪来、怎么连、带什么凭据，都在 ToolSpec.call
  里面，那是 server 的代码。
"""

from atlas_engine.contracts import ModelSpec, ToolResult, ToolSpec, TurnResult
from atlas_engine.runner import run_turn

__all__ = ["ModelSpec", "ToolResult", "ToolSpec", "TurnResult", "run_turn"]
