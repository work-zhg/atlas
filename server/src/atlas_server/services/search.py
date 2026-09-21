"""裸网页搜索工具（SerpAPI）。

模型驱动的检索：`web_search(query)` 只做**一次**搜索并返回结构化结果，
拆词/回环/综合全部交给模型自己 —— 与已移除的 research loop（代码驱动，
见 docs/research-loop.md 的移除记录）是两条路线，本项目当前选这条。

零内部模型调用：不需要 usage.delta；工具经主图 tool node 执行，
事件天然是标准 tool.started/completed，前端无需任何定制。
凭据在 server（G2：engine 零凭据），经 assembly 注入。
"""

from __future__ import annotations

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

SERPAPI_URL = "https://serpapi.com/search.json"
TIMEOUT_S = 15
MAX_RESULTS = 5


class SearchUnavailable(RuntimeError):
    """未配置 SERPAPI_KEY 却勾了 web_search —— 明确报错（§13.2）。"""


class _Args(BaseModel):
    query: str = Field(description="搜索关键词。一次只搜一个主题，需要多角度就多次调用")


async def _serpapi(query: str, api_key: str) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(
            SERPAPI_URL,
            params={"engine": "google", "q": query, "num": MAX_RESULTS, "api_key": api_key},
        )
    resp.raise_for_status()
    return [
        {
            "title": item.get("title", ""),
            "url": item["link"],
            "snippet": item.get("snippet", ""),
        }
        for item in resp.json().get("organic_results", [])[:MAX_RESULTS]
        if "link" in item
    ]


def format_results(query: str, results: list[dict[str, str]]) -> str:
    """给模型看的纯文本：标题 + URL + 摘要。空结果明说，别让模型猜。"""
    if not results:
        return f"「{query}」没有搜索结果。换个关键词试试。"
    lines = [f"「{query}」的搜索结果（{len(results)} 条）："]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)


def make_web_search_tool(api_key: str) -> StructuredTool:
    async def web_search(query: str) -> str:
        try:
            results = await _serpapi(query, api_key)
        except httpx.HTTPStatusError as exc:
            # 错误给模型看：429/401 的处置（换词重试/放弃）由它决定
            raise RuntimeError(f"搜索失败：HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"搜索失败：{exc}") from exc
        return format_results(query, results)

    return StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description=(
            "搜索网页，返回标题/链接/摘要。检索策略由你把控："
            "先拆解问题、从不同角度多次搜索、结果不足就换关键词补搜，"
            "回答时用 markdown 链接标注来源。"
        ),
        args_schema=_Args,
    )
