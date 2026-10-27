"""计量的解析（不起服务）：各协议的用量在哪、流式按事件解析、跨块切开的事件。"""

from __future__ import annotations

import json

from atlas_modelgateway.usage import Meter


def _sse(*events: dict) -> bytes:
    return b"".join(f"event: x\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def test_anthropic_stream_split_across_chunks() -> None:
    raw = _sse(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 2,
                    "output_tokens": 1,
                }
            },
        },
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 42}},
    )
    meter = Meter("messages", limit=1 << 20)
    meter.start("text/event-stream; charset=utf-8")
    for i in range(0, len(raw), 7):  # 七个字节一块：事件被切在任意位置
        meter.feed(raw[i : i + 7])
    u = meter.result()
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens) == (
        10,
        5,
        2,
        42,
    )
    assert u.reported and u.total_input == 17


def test_openai_deepseek_cache_fields() -> None:
    body = {
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 9,
            "prompt_cache_hit_tokens": 20,
            "prompt_cache_miss_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
    }
    meter = Meter("chat", limit=1 << 20)
    meter.start("application/json")
    meter.feed(json.dumps(body).encode())
    u = meter.result()
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens) == (
        30,
        20,
        9,
        4,
    )


def test_openai_stream_with_crlf_and_done() -> None:
    raw = (
        b'data: {"choices":[{"delta":{"content":"a"}}]}\r\n\r\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3}}\r\n\r\n'
        b"data: [DONE]\r\n\r\n"
    )
    meter = Meter("chat", limit=1 << 20)
    meter.start("text/event-stream")
    meter.feed(raw)
    u = meter.result()
    assert (u.input_tokens, u.output_tokens) == (8, 3)


def test_no_usage_is_not_reported_and_overflow_gives_up() -> None:
    meter = Meter("chat", limit=10)
    meter.start("application/json")
    meter.feed(json.dumps({"usage": {"prompt_tokens": 1}}).encode())
    assert not meter.result().reported

    meter = Meter("embeddings", limit=1 << 20)
    meter.start("application/json")
    meter.feed(b'{"data": []}')
    assert not meter.result().reported


def test_rerank_cohere_billed_units() -> None:
    meter = Meter("rerank", limit=1 << 20)
    meter.start("application/json")
    meter.feed(json.dumps({"results": [], "meta": {"billed_units": {"input_tokens": 12}}}).encode())
    u = meter.result()
    assert (u.input_tokens, u.output_tokens, u.reported) == (12, 0, True)
