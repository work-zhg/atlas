"""ToolSpec → LangChain 工具：server 交来的工具在这里、也只在这里变成框架的对象。

★ 失败（ToolResult.is_error）交给模型当作失败的工具结果（status="error"），不打断这一轮；
  call 里抛出的其它异常照旧冒泡 —— 吞掉的话，编程错误会伪装成「服务不可用」被模型绕过去。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from atlas_engine.contracts.tools import ToolContent, ToolSpec

__all__ = ["estimate_tokens", "to_langchain"]


class _ToolFailed(ToolException):
    """ToolResult.is_error：内容原样交给模型（可以是 content blocks）。"""

    def __init__(self, content: ToolContent) -> None:
        super().__init__(content if isinstance(content, str) else "工具调用失败")
        self.content = content


def _failed_content(error: ToolException) -> ToolContent:
    if isinstance(error, _ToolFailed):
        return error.content
    raise error


def to_langchain(spec: ToolSpec) -> BaseTool:
    async def run(**arguments: Any) -> tuple[ToolContent, Any]:
        result = await spec.call(**arguments)
        if isinstance(result, str):
            return result, None
        if result.is_error:
            raise _ToolFailed(result.content)
        return result.content, result.artifact

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.parameters,
        coroutine=run,
        response_format="content_and_artifact",
        # ★ 审批按 metadata["readOnlyHint"] 判只读（tool_registry.tool_effects），键名按 MCP 的写法
        metadata=dict(spec.annotations) or None,
        handle_tool_error=_failed_content,
    )


def estimate_tokens(spec: ToolSpec) -> int:
    """这个工具的定义（名字、描述、参数）进上下文要占多少 token。

    按模型实际看到的形状算（OpenAI 工具格式的 JSON），用来决定工具多时要不要改成按需加载。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from atlas_engine.kernel.context import estimate_text

    try:
        shape = convert_to_openai_tool(to_langchain(spec))
    except Exception:
        return estimate_text(spec.description)
    return estimate_text(json.dumps(shape, ensure_ascii=False))
