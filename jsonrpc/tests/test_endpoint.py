"""RpcEndpoint 的行为约定（代码设计 §2）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from atlas_jsonrpc import ChannelClosed, ErrorCode, RpcEndpoint, RpcFault
from atlas_jsonrpc.memory import MemoryChannel, memory_pair


class Pair:
    """两个通过内存通道相连的端点，各自的读循环在后台运行。"""

    def __init__(self, *, a_req=None, a_note=None, b_req=None, b_note=None) -> None:
        self.ca, self.cb = memory_pair()
        self.a = RpcEndpoint(self.ca, name="a", on_request=a_req, on_notification=a_note)
        self.b = RpcEndpoint(self.cb, name="b", on_request=b_req, on_notification=b_note)

    async def __aenter__(self) -> Pair:
        self.ta = asyncio.create_task(self.a.run())
        self.tb = asyncio.create_task(self.b.run())
        return self

    async def __aexit__(self, *_: object) -> None:
        self.ca.close()
        await asyncio.wait_for(asyncio.gather(self.ta, self.tb), 2)


async def test_request_and_result() -> None:
    async def handler(method: str, params: Any) -> Any:
        return {"echo": method, "params": params}

    async with Pair(b_req=handler) as p:
        assert await p.a.request("hello", {"x": 1}, timeout=1) == {
            "echo": "hello",
            "params": {"x": 1},
        }


async def test_both_directions_can_request() -> None:
    async def a_handler(method: str, params: Any) -> Any:
        return "from a"

    async def b_handler(method: str, params: Any) -> Any:
        return "from b"

    async with Pair(a_req=a_handler, b_req=b_handler) as p:
        assert await p.a.request("x", {}, timeout=1) == "from b"
        assert await p.b.request("x", {}, timeout=1) == "from a"


async def test_handler_fault_becomes_error_response() -> None:
    async def handler(method: str, params: Any) -> Any:
        raise RpcFault(-32012, "忙", {"cause": "turn_busy"})

    async with Pair(b_req=handler) as p:
        with pytest.raises(RpcFault) as info:
            await p.a.request("turn.start", {}, timeout=1)
    assert (info.value.code, info.value.data) == (-32012, {"cause": "turn_busy"})


async def test_unexpected_handler_error_is_internal_error_and_loop_survives() -> None:
    calls = 0

    async def handler(method: str, params: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError("boom")
        return "ok"

    async with Pair(b_req=handler) as p:
        with pytest.raises(RpcFault) as info:
            await p.a.request("x", {}, timeout=1)
        assert info.value.code == ErrorCode.INTERNAL_ERROR
        assert "boom" not in info.value.message  # 内部细节不外泄
        assert await p.a.request("x", {}, timeout=1) == "ok"


async def test_no_request_handler_means_method_not_found() -> None:
    async with Pair() as p:
        with pytest.raises(RpcFault) as info:
            await p.a.request("fs/read_text_file", {}, timeout=1)
    assert info.value.code == ErrorCode.METHOD_NOT_FOUND


async def test_slow_request_does_not_block_reading() -> None:
    """★ 一个要等很久的请求（例如等人决定）不能堵住后续的读取。"""
    release = asyncio.Event()

    async def handler(method: str, params: Any) -> Any:
        if method == "slow":
            await release.wait()
            return "slow done"
        return "fast done"

    async with Pair(b_req=handler) as p:
        slow = asyncio.create_task(p.a.request("slow", {}, timeout=5))
        assert await p.a.request("fast", {}, timeout=1) == "fast done"
        assert not slow.done()
        release.set()
        assert await slow == "slow done"


async def test_notifications_are_handled_in_order_and_serially() -> None:
    """★ 通知串行处理：处理函数在等待时，后面的通知不会被处理 —— 这就是背压的落点。"""
    seen: list[int] = []
    gate = asyncio.Event()

    async def on_note(method: str, params: Any) -> None:
        if params["n"] == 2:
            await gate.wait()
        seen.append(params["n"])

    async with Pair(b_note=on_note) as p:
        for n in range(1, 6):
            await p.a.notify("update", {"n": n})
        await asyncio.sleep(0.05)
        assert seen == [1]  # 卡在第 2 条，3..5 还没处理
        gate.set()
        await asyncio.sleep(0.05)
        assert seen == [1, 2, 3, 4, 5]


async def test_notification_handler_error_does_not_stop_the_loop() -> None:
    seen: list[int] = []

    async def on_note(method: str, params: Any) -> None:
        if params["n"] == 1:
            raise ValueError("bad")
        seen.append(params["n"])

    async with Pair(b_note=on_note) as p:
        await p.a.notify("u", {"n": 1})
        await p.a.notify("u", {"n": 2})
        await asyncio.sleep(0.05)
    assert seen == [2]


async def test_timeout_raises_and_late_response_is_dropped() -> None:
    release = asyncio.Event()

    async def handler(method: str, params: Any) -> Any:
        await release.wait()
        return "late"

    async with Pair(b_req=handler) as p:
        with pytest.raises(TimeoutError):
            await p.a.request("x", {}, timeout=0.05)
        assert p.a._pending == {}
        release.set()
        await asyncio.sleep(0.05)  # 迟到的响应被丢弃，不报错


async def test_start_request_returns_a_future() -> None:
    release = asyncio.Event()

    async def handler(method: str, params: Any) -> Any:
        await release.wait()
        return {"stopReason": "end_turn"}

    async with Pair(b_req=handler) as p:
        future = await p.a.start_request("session/prompt", {})
        await asyncio.sleep(0.05)
        assert not future.done()
        release.set()
        assert await asyncio.wait_for(future, 1) == {"stopReason": "end_turn"}


async def test_close_fails_pending_requests_and_cancels_handlers() -> None:
    handler_cancelled = asyncio.Event()
    started = asyncio.Event()

    async def handler(method: str, params: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    ca, cb = memory_pair()
    a = RpcEndpoint(ca, name="a")
    b = RpcEndpoint(cb, name="b", on_request=handler)
    ta, tb = asyncio.create_task(a.run()), asyncio.create_task(b.run())
    pending = asyncio.create_task(a.request("x", {}, timeout=5))
    await started.wait()
    ca.close()
    with pytest.raises(ChannelClosed):
        await pending
    await asyncio.wait_for(asyncio.gather(ta, tb), 1)
    assert handler_cancelled.is_set()
    assert a.closed and b.closed
    with pytest.raises(ChannelClosed):
        await a.notify("x", {})


async def test_invalid_frames_get_error_replies_but_loop_continues() -> None:
    raw, peer = memory_pair()
    seen: list[str] = []

    async def on_note(method: str, params: Any) -> None:
        seen.append(method)

    endpoint = RpcEndpoint(peer, name="p", on_notification=on_note)
    task = asyncio.create_task(endpoint.run())
    await raw.send("{broken")
    await raw.send('{"jsonrpc":"2.0","id":4,"method":"x","params":1}')
    await raw.send('{"jsonrpc":"2.0","id":5}')  # 形状像响应：不回
    await raw.send('{"jsonrpc":"2.0","method":"still-alive"}')
    replies = [json.loads(await asyncio.wait_for(raw.receive(), 1)) for _ in range(2)]
    assert [(r["id"], r["error"]["code"]) for r in replies] == [
        (None, ErrorCode.PARSE_ERROR),
        (4, ErrorCode.INVALID_REQUEST),
    ]
    await asyncio.sleep(0.05)
    assert seen == ["still-alive"]
    raw.close()
    await asyncio.wait_for(task, 1)


async def test_concurrent_sends_are_not_interleaved() -> None:
    """同时发出的多条消息，每条都完整到达。"""

    class SlowChannel(MemoryChannel):
        async def send(self, text: str) -> None:
            await asyncio.sleep(0)  # 让出执行权，模拟写入途中的等待
            await super().send(text)

    a, b = SlowChannel(), MemoryChannel()
    a._peer, b._peer = b, a
    endpoint = RpcEndpoint(a, name="a")
    await asyncio.gather(*(endpoint.notify("n", {"i": i}) for i in range(50)))
    received = [json.loads(await b.receive())["params"]["i"] for _ in range(50)]
    assert sorted(received) == list(range(50))


async def test_current_request_id_is_visible_inside_the_handler() -> None:
    from atlas_jsonrpc import current_request_id

    seen: list[object] = []

    async def handler(method: str, params: object) -> object:
        await asyncio.sleep(0)
        seen.append(current_request_id())
        return {}

    a, b = memory_pair()
    server = RpcEndpoint(a, name="server", on_request=handler)
    client = RpcEndpoint(b, name="client")
    tasks = [asyncio.create_task(server.run()), asyncio.create_task(client.run())]
    await asyncio.gather(client.request("x", {}, timeout=1), client.request("y", {}, timeout=1))
    assert sorted(seen) == [1, 2]  # 并发的两个请求各自看到自己的 id
    assert current_request_id() is None
    a.close()
    await asyncio.gather(*tasks, return_exceptions=True)
