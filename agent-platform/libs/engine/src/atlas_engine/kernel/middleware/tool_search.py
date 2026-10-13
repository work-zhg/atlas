"""工具按需加载（doc/context-management-design.html §13）。

MCP 工具多的时候，它们的完整定义（名字、描述、参数 schema）常驻上下文是一笔不小的固定开销。
这里把它们改为「延迟工具」：

    · 模型默认只看到名字 + 一句描述（追加在系统提示词末尾，每次调用都相同 —— 不影响缓存）
    · 需要时调 tool_search(query) 搜索；命中的工具在**之后的调用里**带上完整定义，可以直接调用
    · 已加载集合从历史里 tool_search 的结果推出来（不存额外状态）—— 续跑、重建历史都成立

★ 工具节点里所有工具都注册着：模型一旦拿到定义就能调用，执行路径与普通工具完全一样。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

__all__ = ["TOOL_SEARCH", "ToolSearchMiddleware"]

TOOL_SEARCH = "tool_search"
#: tool_search 结果里标记已加载工具的一行（解析它来恢复已加载集合）
_LOADED = re.compile(r"^\[loaded\] (.+)$", re.M)
_MAX_RESULTS = 5


class _Args(BaseModel):
    query: str = Field(
        description=(
            "要找的能力的关键词（如「发送邮件」「jira issue」）；"
            "或 `select:工具名1,工具名2` 直接按名字加载。"
        )
    )


def _brief(tool: BaseTool) -> str:
    desc = (tool.description or "").strip().splitlines()[0] if tool.description else ""
    return desc[:120]


class ToolSearchMiddleware(AgentMiddleware):
    def __init__(self, deferred: Sequence[BaseTool]) -> None:
        super().__init__()
        self._deferred = {t.name: t for t in deferred}
        self.tools: list[BaseTool] = [self._make_search_tool()]

    # ------------------------------------------------------------------ 搜索

    def search(self, query: str) -> list[BaseTool]:
        q = query.strip()
        if q.lower().startswith("select:"):
            names = [n.strip() for n in q[7:].split(",") if n.strip()]
            return [self._deferred[n] for n in names if n in self._deferred]
        terms = [t for t in re.split(r"[\s,，、/]+", q.lower()) if t]
        if not terms:
            return []
        scored = []
        for tool in self._deferred.values():
            name = tool.name.lower()
            text = f"{name} {(tool.description or '').lower()}"
            score = sum(3 if term in name else 1 for term in terms if term in text)
            if score:
                scored.append((score, tool.name, tool))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [t for _, _, t in scored[:_MAX_RESULTS]]

    def _make_search_tool(self) -> BaseTool:
        def run(query: str) -> str:
            hits = self.search(query)
            if not hits:
                return f"没有找到与「{query}」匹配的工具。可用的延迟工具见系统提示词里的清单。"
            lines = [f"找到 {len(hits)} 个工具，已加载，下一步即可直接调用："]
            for t in hits:
                lines.append(f"- {t.name}：{_brief(t)}")
            lines.append(f"[loaded] {','.join(t.name for t in hits)}")
            return "\n".join(lines)

        async def arun(query: str) -> str:
            return run(query)

        return StructuredTool.from_function(
            name=TOOL_SEARCH,
            description=(
                "按需加载工具：系统提示词里列出的「延迟工具」只有名字，调用前先用它加载完整定义。"
                "可以按关键词搜索，也可以用 select:名字1,名字2 直接加载。"
            ),
            func=run,
            coroutine=arun,
            args_schema=_Args,
        )

    # ------------------------------------------------------------------ 每次调用

    def _loaded(self, messages: Sequence[Any]) -> set[str]:
        out: set[str] = set()
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == TOOL_SEARCH:
                text = m.content if isinstance(m.content, str) else str(m.content)
                for match in _LOADED.finditer(text):
                    out.update(n.strip() for n in match.group(1).split(","))
        return out & set(self._deferred)

    def _catalog(self) -> str:
        lines = [f"- {name}：{_brief(t)}" for name, t in sorted(self._deferred.items())]
        return (
            "\n\n【延迟工具】以下工具只列了名字，调用前先用 tool_search 加载它的完整定义"
            "（加载一次，本会话之后都可用）：\n" + "\n".join(lines)
        )

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        loaded = self._loaded(request.messages)
        tools = [
            t
            for t in request.tools
            if not (getattr(t, "name", None) in self._deferred and t.name not in loaded)
        ]
        system = request.system_message
        catalog = self._catalog()
        if system is None:
            new_system = SystemMessage(content=catalog.strip())
        elif isinstance(system.content, str):
            new_system = SystemMessage(content=system.content + catalog)
        else:  # content blocks（如带 cache_control 的块）：追加一个文本块，不动原有的
            new_system = SystemMessage(content=[*system.content, {"type": "text", "text": catalog}])
        return await handler(request.override(tools=tools, system_message=new_system))
