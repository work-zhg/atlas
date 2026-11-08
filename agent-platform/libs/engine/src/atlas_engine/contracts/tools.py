"""server 交给原生运行时的工具：只有名字、描述、参数定义和一个异步函数（与框架无关）。

MCP、知识库、记忆这些工具由 server 实现（它们要连外部服务、带身份），原生运行时把它们转成
框架的工具对象（runtime/tools.py）。server 因此不需要知道 LangChain。

★ 身份、库 id、会话这些不进参数定义，由 call 闭包捕获：一旦模型能指定，就是冒用的口子。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

__all__ = ["ToolContent", "ToolResult", "ToolSpec"]

#: 交给模型的工具结果：一段文本，或 content blocks（{"type": "text", "text": …}、
#: {"type": "image", "base64": …, "mime_type": …}、{"type": "file", "url": …} 等标准形状）
ToolContent = str | list[dict[str, Any]]


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    is_error=True：调用失败但不打断这一轮 —— 内容作为失败的工具结果交给模型，说明能不能重试、
    该找谁。artifact 只给程序看（事件、落库），不进模型的上下文。
    """

    content: ToolContent
    artifact: Any = None
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """一个工具。

    name          模型看到的名字
    description   模型看到的说明
    parameters    参数定义：pydantic 模型，或 JSON Schema（对象）
    call          按关键字参数调用，返回 ToolResult（或一段文本，等同于成功的 ToolResult）
    annotations   工具的注解（MCP 的 readOnlyHint / destructiveHint 等，键名按 MCP 线上的写法）。
                  审批模式据此判副作用类别（tool_registry.tool_effects）
    """

    name: str
    description: str
    parameters: type[BaseModel] | dict[str, Any]
    call: Callable[..., Awaitable[ToolResult | str]]
    annotations: Mapping[str, Any] = field(default_factory=dict)
