"""测试用的 Higress AI 路由替身：按请求体里的模型名路由到假的模型。

    /v1/messages             Anthropic Messages（流式 / 非流式；工具调用；思考；缓存）
    /v1/chat/completions     OpenAI Chat Completions（流式时只有带 include_usage 才给用量）
    /v1/embeddings           4 维向量
    /v1/rerank               Cohere 兼容

★ 和真实 Higress 一样只认网关凭据（Authorization: Bearer <凭据>），open=True 的模型除外
  （演示「Higress 上漏开了认证」）。
★ 每个模型一份 Profile：支持哪些协议、哪些能力；不支持的参数回 400（同真实网关的「传了就 400」）。
★ 在后台线程里起 uvicorn（自己的 event loop），网关经真实的 HTTP 调它。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


@dataclass
class Profile:
    protocols: tuple[str, ...] = ("anthropic", "openai")
    kind: str = "chat"
    #: tools / parallel_tools / thinking_adaptive / thinking_budget / thinking_disabled / effort /
    #: temperature / cache / vision / structured_output
    supports: frozenset[str] = frozenset({"tools", "temperature"})
    open: bool = False
    #: 非空 = 一律回这个状态码（402 额度用完、500 …）
    fail_with: int | None = None
    #: 流式时回报用量
    stream_usage: bool = True
    usage: dict[str, int] = field(
        default_factory=lambda: {"input": 100, "output": 20, "cache_read": 30, "cache_write": 0}
    )


@dataclass
class FakeLLM:
    credential: str
    models: dict[str, Profile] = field(default_factory=dict)
    #: 每个请求：(路径, 请求头, 请求体)
    seen: list[tuple[str, dict[str, str], dict[str, Any]]] = field(default_factory=list)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/messages", self._messages, methods=["POST"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                Route("/v1/rerank", self._rerank, methods=["POST"]),
            ]
        )

    async def _admit(
        self, request: Request, protocol: str
    ) -> tuple[Profile | None, dict[str, Any], Response | None]:
        body = json.loads(await request.body() or b"{}")
        self.seen.append((request.url.path, dict(request.headers), body))
        profile = self.models.get(str(body.get("model")))
        if profile is None:
            return None, body, JSONResponse({"error": "no route"}, status_code=404)
        if not profile.open and request.headers.get("authorization") != f"Bearer {self.credential}":
            return None, body, JSONResponse({"message": "unauthorized"}, status_code=401)
        if protocol not in profile.protocols:
            return None, body, JSONResponse({"error": "protocol"}, status_code=400)
        if profile.fail_with is not None:
            return (
                None,
                body,
                JSONResponse({"error": {"message": "fail"}}, status_code=profile.fail_with),
            )
        return profile, body, None

    # ── Anthropic

    async def _messages(self, request: Request) -> Response:
        profile, body, denied = await self._admit(request, "anthropic")
        if denied is not None:
            return denied
        assert profile is not None
        bad = self._unsupported_anthropic(profile, body)
        if bad:
            return JSONResponse(
                {"type": "error", "error": {"type": "invalid_request_error", "message": bad}},
                status_code=400,
            )
        u = profile.usage
        cache_read = u["cache_read"] if "cache" in profile.supports and body.get("system") else 0
        usage = {
            "input_tokens": u["input"],
            "output_tokens": u["output"],
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": u["cache_write"],
        }
        tools = body.get("tools") or []
        last = (body.get("messages") or [{}])[-1]
        answered = isinstance(last.get("content"), list) and any(
            b.get("type") == "tool_result" for b in last["content"]
        )
        content: list[dict[str, Any]]
        if tools and not answered and "tools" in profile.supports:
            n = (
                2
                if "parallel_tools" in profile.supports
                and "同时" in json.dumps(body, ensure_ascii=False)
                else 1
            )
            content = [
                {
                    "type": "tool_use",
                    "id": f"toolu_{i}",
                    "name": tools[0]["name"],
                    "input": {"city": "北京"},
                }
                for i in range(n)
            ]
        else:
            content = [{"type": "text", "text": "好的"}]
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": content,
                    "stop_reason": "end_turn",
                    "usage": usage,
                }
            )

        def events() -> Iterator[bytes]:
            start_usage = {k: v for k, v in usage.items() if k != "output_tokens"} | {
                "output_tokens": 1
            }
            yield _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": body["model"],
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": start_usage if profile.stream_usage else {},
                    },
                },
            )
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "好的"},
                },
            )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            delta: dict[str, Any] = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
            if profile.stream_usage:
                delta["usage"] = {"output_tokens": usage["output_tokens"]}
            yield _sse("message_delta", delta)
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(events(), media_type="text/event-stream")

    @staticmethod
    def _unsupported_anthropic(profile: Profile, body: dict[str, Any]) -> str:
        thinking = body.get("thinking") or {}
        if thinking.get("type") == "adaptive" and "thinking_adaptive" not in profile.supports:
            return "adaptive thinking is not supported on this model"
        if thinking.get("type") == "enabled" and "thinking_budget" not in profile.supports:
            return "thinking is not supported"
        if thinking.get("type") == "disabled" and "thinking_disabled" not in profile.supports:
            return "thinking cannot be disabled"
        if "output_config" in body and "effort" not in profile.supports:
            return "This model does not support the effort parameter."
        if "temperature" in body and "temperature" not in profile.supports:
            return "`temperature` is deprecated for this model."
        images = [
            b
            for m in body.get("messages") or []
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "image"
        ]
        if images and "vision" not in profile.supports:
            return "image input is not supported"
        return ""

    # ── OpenAI

    async def _chat(self, request: Request) -> Response:
        profile, body, denied = await self._admit(request, "openai")
        if denied is not None:
            return denied
        assert profile is not None
        if "temperature" in body and "temperature" not in profile.supports:
            return JSONResponse({"error": {"message": "temperature"}}, status_code=400)
        if "response_format" in body and "structured_output" not in profile.supports:
            return JSONResponse({"error": {"message": "response_format"}}, status_code=400)
        u = profile.usage
        cached = u["cache_read"] if "cache" in profile.supports else 0
        usage = {
            "prompt_tokens": u["input"] + cached,
            "completion_tokens": u["output"],
            "prompt_tokens_details": {"cached_tokens": cached},
        }
        tools = body.get("tools") or []
        last = (body.get("messages") or [{}])[-1]
        message: dict[str, Any] = {"role": "assistant", "content": "好的"}
        if tools and last.get("role") != "tool" and "tools" in profile.supports:
            n = (
                2
                if "parallel_tools" in profile.supports
                and "同时" in json.dumps(body, ensure_ascii=False)
                else 1
            )
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tools[0]["function"]["name"],
                            "arguments": '{"city": "北京"}',
                        },
                    }
                    for i in range(n)
                ],
            }
        if "response_format" in body:
            message["content"] = '{"city": "北京", "country": "中国"}'
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": "c1",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": usage,
                }
            )
        include = (
            bool((body.get("stream_options") or {}).get("include_usage")) and profile.stream_usage
        )

        def chunks() -> Iterator[bytes]:
            yield _data(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "好的"}}],
                }
            )
            yield _data(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            if include:
                yield _data(
                    {"id": "c1", "object": "chat.completion.chunk", "choices": [], "usage": usage}
                )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async def _embeddings(self, request: Request) -> Response:
        _, body, denied = await self._admit(request, "openai")
        if denied is not None:
            return denied
        texts = body.get("input") or []
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]} for i, _ in enumerate(texts)
                ],
                "usage": {"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)},
            }
        )

    async def _rerank(self, request: Request) -> Response:
        _, body, denied = await self._admit(request, "rerank")
        if denied is not None:
            return denied
        docs = body.get("documents") or []
        return JSONResponse(
            {
                "results": [
                    {"index": i, "relevance_score": 1.0 / (i + 1)} for i, _ in enumerate(docs)
                ],
                "usage": {"total_tokens": 11},
            }
        )


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _data(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@contextmanager
def serve_in_thread(app: Any) -> Iterator[str]:
    """在后台线程里起 uvicorn，返回地址；退出时停掉。"""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("假的 Higress 没起来")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
