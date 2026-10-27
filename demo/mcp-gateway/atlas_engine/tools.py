"""ToolSpec → LangChain 工具。

★ 这是唯一把 server 交来的工具变成 LangChain 对象的地方；server 那边不 import LangChain。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from atlas_engine.contracts import ToolSpec


def to_langchain(spec: ToolSpec) -> BaseTool:
    async def run(**arguments: Any) -> str:
        result = await spec.call(arguments)
        if result.is_error:
            raise ToolException(result.text)  # → ToolMessage(status="error")，模型能看到原因
        return result.text

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.input_schema,  # JSON Schema 直接给，LangChain 转成各家模型的格式
        coroutine=run,
        handle_tool_error=True,
        metadata={"readOnlyHint": spec.read_only},
    )
