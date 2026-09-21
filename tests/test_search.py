"""裸网页搜索工具（services/search.py）。SerpAPI 真实调用在 live（gated）。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_server.services import search as search_mod
from atlas_server.services.search import format_results, make_web_search_tool


async def test_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(query: str, api_key: str) -> list[dict[str, str]]:
        assert api_key == "k"
        return [{"title": "Python 3.13 发布", "url": "https://py.org/1", "snippet": "2024-10-07"}]

    monkeypatch.setattr(search_mod, "_serpapi", fake)
    out = await make_web_search_tool("k").ainvoke({"query": "python 3.13"})
    assert "Python 3.13 发布" in out
    assert "https://py.org/1" in out


async def test_empty_results_say_so() -> None:
    assert "没有搜索结果" in format_results("冷门词", [])


async def test_http_error_surfaces_to_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """429/401 要让模型看到并自行处置，不静默吞掉。"""

    async def fake(query: str, api_key: str) -> Any:
        request = httpx.Request("GET", "https://serpapi.com")
        raise httpx.HTTPStatusError(
            "rate limited", request=request, response=httpx.Response(429, request=request)
        )

    monkeypatch.setattr(search_mod, "_serpapi", fake)
    with pytest.raises(RuntimeError, match="429"):
        await make_web_search_tool("k").ainvoke({"query": "x"})


def test_registry_still_lists_web_search() -> None:
    from atlas_server.domain.spec import AgentSpec, ModelSpec
    from atlas_server.domain.tool_registry import unsupported_tools

    spec = AgentSpec(
        slug="s",
        name="n",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("web_search",),
    )
    assert unsupported_tools(spec) == []
