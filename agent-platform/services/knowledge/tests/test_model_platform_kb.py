"""知识库经模型管理平台（agent-platform/doc/design/model-design.html 第 9 章）：
各用途按「类型:编码」取服务级客户端 Key，带着它直连 LiteLLM；Key 被重置时 401 → 重取再试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from atlas_knowledge.rag.model_platform import (
    PlatformChat,
    PlatformKeyAuth,
    PlatformUnavailable,
    ServiceKeys,
    parse_client,
)
from atlas_knowledge.rag.remote_models import OpenAICompatibleEmbedding

CHAT = ("Service", "knowledge-chat")
EMBEDDING = ("Service", "knowledge-embedding")


class FakePlatform:
    """平台的内部接口 GET /internal/client-keys：类型 + 编码 → 状态、原文、可用模型。"""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], dict[str, Any]] = {}
        self.fetches = 0
        self.down = False

    def assign(self, client: tuple[str, str], models: list[str], secret: str) -> None:
        self.keys[client] = {
            "status": "active",
            "key": secret,
            "models": [{"name": m} for m in models],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        assert request.headers["x-caller-app"] == "atlas"
        if self.down:
            return httpx.Response(503)
        slot = (request.url.params["client_type"], request.url.params["client_code"])
        if slot not in self.keys:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=self.keys[slot])


class FakeLiteLLM:
    """只认 valid 里的 Key，别的回 401（同 LiteLLM 的 token_not_found_in_db）。"""

    def __init__(self, *valid: str) -> None:
        self.valid = set(valid)
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(401, json={"error": {"type": "token_not_found_in_db"}})
        body = json.loads(request.content)
        if request.url.path == "/v1/embeddings":
            data = [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]
            return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 3}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def keys(platform: FakePlatform) -> ServiceKeys:
    return ServiceKeys(
        "http://platform",
        app_code="atlas",
        ttl_s=60,
        transport=httpx.MockTransport(platform.handle),
    )


def test_parse_client() -> None:
    assert parse_client("Service:knowledge-chat") == CHAT
    assert parse_client(None) is None and parse_client("") is None
    with pytest.raises(ValueError):
        parse_client("knowledge-chat")


@pytest.mark.asyncio
async def test_chat_calls_litellm_with_the_service_key(
    platform: FakePlatform, keys: ServiceKeys
) -> None:
    litellm = FakeLiteLLM("sk-chat")
    chat = PlatformChat(
        "http://litellm:4000", keys, CHAT, "chat-a", transport=httpx.MockTransport(litellm.handle)
    )
    # 平台上还没有这把 Key：抛出，调用方按「LLM 不可用」降级
    with pytest.raises(PlatformUnavailable, match="missing"):
        await chat.complete([{"role": "user", "content": "x"}])

    # 不可用的结果也短缓存：管理员配好之后最多滞后 ttl
    platform.assign(CHAT, ["chat-b"], "sk-chat")
    keys.get(CHAT, fresh=True)
    with pytest.raises(PlatformUnavailable, match="没有分配模型 chat-a"):
        await chat.complete([{"role": "user", "content": "x"}])

    platform.assign(CHAT, ["chat-a"], "sk-chat")
    keys.get(CHAT, fresh=True)
    assert await chat.complete([{"role": "user", "content": "x"}]) == "好"
    call = litellm.seen[-1]
    assert str(call.url) == "http://litellm:4000/v1/chat/completions"
    assert call.headers["authorization"] == "Bearer sk-chat"
    assert json.loads(call.content)["model"] == "chat-a"
    await chat.aclose()


@pytest.mark.asyncio
async def test_chat_refetches_the_key_after_a_reset(
    platform: FakePlatform, keys: ServiceKeys
) -> None:
    platform.assign(CHAT, ["chat-a"], "sk-old")
    litellm = FakeLiteLLM("sk-old")
    chat = PlatformChat(
        "http://litellm:4000", keys, CHAT, "chat-a", transport=httpx.MockTransport(litellm.handle)
    )
    assert await chat.complete([{"role": "user", "content": "x"}]) == "好"

    # 管理员重置了 Key：缓存里还是旧的，LiteLLM 回 401 → 重取一次再试
    platform.assign(CHAT, ["chat-a"], "sk-new")
    litellm.valid = {"sk-new"}
    assert await chat.complete([{"role": "user", "content": "x"}]) == "好"
    assert [r.headers["authorization"] for r in litellm.seen[-2:]] == [
        "Bearer sk-old",
        "Bearer sk-new",
    ]
    await chat.aclose()


def test_remote_embedding_uses_the_service_key_and_survives_a_reset(
    platform: FakePlatform, keys: ServiceKeys
) -> None:
    platform.assign(EMBEDDING, ["emb-a"], "sk-old")
    litellm = FakeLiteLLM("sk-old")
    emb = OpenAICompatibleEmbedding(
        "emb-a",
        base_url="http://litellm:4000/v1",
        transport=httpx.MockTransport(litellm.handle),
        auth=PlatformKeyAuth(keys, EMBEDDING),
    )
    assert emb.dim == 2
    assert litellm.seen[-1].url.path == "/v1/embeddings"
    assert litellm.seen[-1].headers["authorization"] == "Bearer sk-old"

    platform.assign(EMBEDDING, ["emb-a"], "sk-new")
    litellm.valid = {"sk-new"}
    assert emb.embed_documents(["一段文字"])[0] == pytest.approx([0.1, 0.2])
    assert litellm.seen[-1].headers["authorization"] == "Bearer sk-new"


def test_platform_outage_keeps_the_last_key(platform: FakePlatform, keys: ServiceKeys) -> None:
    platform.assign(CHAT, ["chat-a"], "sk-chat")
    assert keys.get(CHAT).key == "sk-chat"
    platform.down = True
    assert keys.get(CHAT, fresh=True).key == "sk-chat"
    # 从没取到过的：平台不可用就是不可用
    with pytest.raises(PlatformUnavailable):
        keys.get(EMBEDDING)
    # 短缓存：缓存期内不再请求平台
    platform.down = False
    before = platform.fetches
    keys.get(CHAT)
    assert platform.fetches == before


@pytest.mark.asyncio
async def test_meta_lists_the_models_of_the_service_keys(
    rt: Any, client: Any, platform: FakePlatform, keys: ServiceKeys
) -> None:
    rt.service_keys = keys
    rt.settings = rt.settings.model_copy(
        update={
            "embedding_client": "Service:knowledge-embedding",
            "rerank_client": "Service:knowledge-rerank",
        }
    )
    platform.assign(EMBEDDING, ["emb-a", "emb-b"], "sk-emb")
    meta = (await client.get("/v1/meta", headers={"X-Uid": "alice"})).json()
    remote = [m["model"] for m in meta["embedding_models"] if m["provider"] == "openai_compatible"]
    assert remote == ["emb-a", "emb-b"]
    # 重排的 Key 平台上还没有：只列本地模型
    assert [m["provider"] for m in meta["rerank_models"]] == ["fastembed"]
