"""知识库经模型网关（doc/model-design.html §6.7）：按平台用途调，不带 Key、不写死模型名。"""

from __future__ import annotations

import json

import httpx
import pytest
from atlas_knowledge.rag.model_gateway import PurposeCatalog, PurposeChat, purpose_headers
from atlas_knowledge.rag.remote_models import OpenAICompatibleEmbedding

CATALOG = {
    "models": [],
    "purposes": [
        {"code": "kb_enrich", "default_model": "chat-a", "allowed_models": ["chat-a"]},
        {"code": "kb_query", "default_model": None, "allowed_models": []},
        {"code": "kb_embedding", "default_model": "emb-a", "allowed_models": ["emb-a", "emb-b"]},
    ],
}


@pytest.mark.asyncio
async def test_purpose_chat_uses_the_configured_model_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/internal/catalog":
            return httpx.Response(200, json=CATALOG)
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    transport = httpx.MockTransport(handler)
    catalog = PurposeCatalog("http://gw", transport=transport)
    chat = PurposeChat("http://gw", "kb_enrich", catalog, app_code="atlas", transport=transport)
    assert await chat.complete([{"role": "user", "content": "x"}]) == "好"
    call = seen[-1]
    assert call.url.path == "/v1/chat/completions"
    assert json.loads(call.content)["model"] == "chat-a" and chat.model == "chat-a"
    assert call.headers["x-model-purpose"] == "kb_enrich"
    assert call.headers["x-caller-app"] == "atlas"
    assert "authorization" not in call.headers  # 平台用途不带 Key

    # 用途还没配模型：抛出，调用方按「LLM 不可用」降级
    query = PurposeChat("http://gw", "kb_query", catalog, app_code="atlas", transport=transport)
    with pytest.raises(RuntimeError, match="还没有配置模型"):
        await query.complete([{"role": "user", "content": "x"}])
    assert await catalog.options("kb_embedding") == ["emb-a", "emb-b"]
    await chat.aclose()
    await query.aclose()


def test_remote_embedding_sends_purpose_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        n = len(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(n)],
                "usage": {"total_tokens": 3},
            },
        )

    emb = OpenAICompatibleEmbedding(
        "emb-a",
        base_url="http://gw/v1",
        transport=httpx.MockTransport(handler),
        headers=purpose_headers("kb_embedding", "atlas"),
    )
    assert emb.dim == 2
    assert seen[-1].url.path == "/v1/embeddings"
    assert seen[-1].headers["x-model-purpose"] == "kb_embedding"
    assert "authorization" not in seen[-1].headers
