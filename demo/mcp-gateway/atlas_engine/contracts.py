"""engine 与 server 之间的契约：只有标准库类型。

server 按这里的形状交工具、交模型规格，拿回结果；不需要知道 LangChain。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。is_error=True 时内容交给模型作为失败的工具结果，不打断这一轮。"""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """一个工具：engine 只看名字、描述、参数定义和副作用类别，执行交给 call。"""

    name: str  # 模型看到的名字
    description: str
    input_schema: dict[str, Any]  # JSON Schema（对象）
    call: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    read_only: bool = False  # 副作用类别：审批按它判（demo 没有审批）


@dataclass(frozen=True)
class ModelSpec:
    provider: str  # 本例只有 "mock"
    name: str


@dataclass(frozen=True)
class TurnResult:
    answer: str
    #: 这一轮的消息摘要：(角色, 文本)。不把 LangChain 的消息对象交出去
    transcript: list[tuple[str, str]] = field(default_factory=list)
