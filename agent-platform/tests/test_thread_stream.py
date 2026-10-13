"""会话级事件流 `GET /v1/threads/{id}/events`（S3）。

订阅单位从 run 换成 thread。这解决的是一个结构性缺陷：前端只订阅父 run 那
一条流，而子 run 的过程活在它自己的流上 —— 父 run 在委派处挂起之后，父流
再不产出任何事件，于是审批弹窗永远不出现
（doc/detail/suspension.html §04/§06）。

这里测四件容易出错的事：

  永不自动结束   终态事件不再是关闭信号 —— 一轮跑完后面还有下一轮
  游标口径       id 写 thread_seq，不是 run 内的 seq。混了就会整段错位
  首连限量       一条会话可以有几百轮，从 0 回放会把前端灌死
  归档接力       Redis 的流有 maxlen，早期事件只在 Postgres 里
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from atlas_server.config import Settings, get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.domain.events import TraceEvent
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from atlas_server.stream.gate import StreamGate, StreamsBusy
from httpx import ASGITransport

from .fakes import text_model

pytestmark = pytest.mark.usefixtures("clean_db")


def make_app(*, chunks: list[str] | None = None):
    app = create_app()
    model = text_model("".join(chunks or ["好"]), pieces=len(chunks or ["好"]))
    app.state.executor = InProcessExecutor(
        get_sessionmaker(), get_settings(), model_builder=lambda *_a, **_k: model
    )
    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def setup_thread(c: httpx.AsyncClient) -> str:
    agent = await c.post(
        "/v1/agents",
        json={
            "slug": "plain",
            "name": "朴素",
            "spec": {"system_prompt": "你好", "model": {"model": "claude-sonnet-5"}},
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await c.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    return thread.json()["id"]


async def run_once(c: httpx.AsyncClient, thread_id: str, text: str = "hi") -> str:
    rid = (
        await c.post(
            f"/v1/threads/{thread_id}/runs", json={"content": [{"type": "text", "text": text}]}
        )
    ).json()["run_id"]
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        body = (await c.get(f"/v1/runs/{rid}")).json()
        if body["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return rid
        await asyncio.sleep(0.05)
    raise AssertionError("run 超时")


def parse_sse(text: str) -> list[tuple[str, str, dict]]:
    """→ [(id, event, data)]。"""
    out: list[tuple[str, str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        eid = etype = ""
        data = "{}"
        for line in block.splitlines():
            if line.startswith("id: "):
                eid = line[4:]
            elif line.startswith("event: "):
                etype = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        out.append((eid, etype, json.loads(data)))
    return out


async def read_stream(
    thread_id: str,
    *,
    after_seq: int | None,
    seconds: float = 1.5,
    settings: Settings | None = None,
) -> tuple[list[TraceEvent], bool]:
    """直接迭代 service 的会话流生成器。返回 (事件, 生成器是否自行结束)。

    ★ 为什么不走 HTTP。httpx 的 ASGITransport 在请求体读完后就让 `receive`
      返回 `http.disconnect`，而 Starlette 的 StreamingResponse 有一个
      listen_for_disconnect 任务盯着它 —— 收到就取消整个响应。对**会自然结束**
      的 run 流没影响（回放完就 return，赶在取消之前），但会话流**永不结束**，
      于是一帧都发不出来（实测 0 chunks，测试直接挂死）。
      这是测试传输层的限制，不是产品行为 —— 真实的 uvicorn 只在客户端真的断开
      时才发 disconnect。

    ★ HTTP 层的接线另有覆盖：路由注册（openapi）、404 走非流式 GET、并发闸门
      是纯逻辑单测。这里测的是流本身的语义。

    ★ 「生成器自行结束」= StopAsyncIteration。会话流正确的行为是**不**结束，
      所以它为 True 反而说明出了问题。
    """
    from atlas_server.redisx import make_redis
    from atlas_server.services.run import RunService

    conf = settings or get_settings()
    events: list[TraceEvent] = []
    finished = False
    redis = make_redis(conf)
    try:
        async with get_sessionmaker()() as session:
            service = RunService(session, redis, conf, None)  # type: ignore[arg-type]
            gen = service.stream_thread(UUID(thread_id), after_seq=after_seq)
            try:
                async with asyncio.timeout(seconds):
                    async for frame in gen:
                        if frame.startswith(":"):
                            continue  # 心跳
                        events.append(_event_of(frame))
                    finished = True
            except (TimeoutError, asyncio.CancelledError):
                pass
            finally:
                await gen.aclose()
    finally:
        await redis.aclose()
    return events, finished


def _event_of(frame: str) -> TraceEvent:
    """一个 SSE 帧 → TraceEvent，顺带校验 id 行与 payload 自洽。"""
    parsed = parse_sse(frame)
    assert len(parsed) == 1, frame
    eid, _etype, data = parsed[0]
    event = TraceEvent.model_validate(data)
    assert eid == str(event.thread_seq), f"id 写的不是 thread_seq：{eid} vs {event}"
    return event


async def iter_stream(thread_id: str, *, after_seq: int | None, seconds: float = 5.0):
    """逐个产出会话流上的事件，直到调用方 break 或超时。

    ★ 与 read_stream 的差别是「能提前停」—— 测「收到某个事件的**那一刻**
      DB 是什么状态」必须能在事件到达时立刻跳出，而不是等流读完。
    """
    from atlas_server.redisx import make_redis
    from atlas_server.services.run import RunService

    conf = get_settings()
    redis = make_redis(conf)
    try:
        async with get_sessionmaker()() as session:
            service = RunService(session, redis, conf, None)  # type: ignore[arg-type]
            gen = service.stream_thread(UUID(thread_id), after_seq=after_seq)
            try:
                async with asyncio.timeout(seconds):
                    async for frame in gen:
                        if frame.startswith(":"):
                            continue
                        yield _event_of(frame)
            except (TimeoutError, asyncio.CancelledError):
                pass
            finally:
                await gen.aclose()
    finally:
        await redis.aclose()


# ──────────────────────────────────────────────── 基本形状


async def test_thread_stream_replays_a_finished_run(client) -> None:
    tid = await setup_thread(client)
    await run_once(client, tid)

    # 会话流不自动结束 —— 用 asyncio.timeout 掐断，拿到已回放的部分
    events, _finished = await read_stream(tid, after_seq=None)

    types = [e.type for e in events]
    assert "run.started" in types
    assert "run.finished" in types


async def test_the_cursor_is_thread_seq_not_run_seq(client) -> None:
    """★ id 必须写 thread_seq。

    两个序号都是小整数，混了不会报错 —— 只是重连时游标被拿到另一个体系里
    比较，补发范围整个错位（重复一大段或静默丢一大段）。
    """
    tid = await setup_thread(client)
    await run_once(client, tid)

    events, _finished = await read_stream(tid, after_seq=None)

    # id 行与 thread_seq 的一致性由 _event_of 逐帧断言
    assert events
    assert all(e.thread_seq > 0 for e in events)


async def test_terminal_event_does_not_close_the_thread_stream(client) -> None:
    """★ 核心性质：run.finished 之后流还开着。

    关掉的后果是用户发第二句话时流已经断了 —— 而会话流的全部意义就是跨轮次
    存在（子 run 的过程、挂起期间的审批都在别的 run 上）。
    """
    tid = await setup_thread(client)
    await run_once(client, tid)

    events, finished = await read_stream(tid, after_seq=None, seconds=2.0)

    assert "run.finished" in [e.type for e in events]
    # ★ 判据：生成器**没有**自行结束。结束了就意味着下一轮的事件再也收不到。
    assert not finished, "会话流在终态事件后结束了 —— 它本该跟着会话一直活着"


async def test_404_is_json_not_an_empty_stream(client) -> None:
    """不存在的会话要给 JSON 404，而不是一个 200 的空流。

    ★ 生成器里抛的异常已经在响应体里了 —— 必须在构造流**之前**探一次。
    """
    missing = "11111111-2222-3333-4444-555555555555"
    r = await client.get(f"/v1/threads/{missing}/events")
    assert r.status_code == 404
    assert r.json()["error"]["kind"] == "not_found"


# ──────────────────────────────────────────────── 顺序保证


async def test_terminal_event_is_published_after_persist(client) -> None:
    """★ 看到终止事件 ⇒ DB 已是最终状态。

    前端收到 run.finished 会立刻回查最终用量。曾经的顺序是「先发布终止事件、
    后落库」，于是这一查读到 status=running、tokens=0、last_seq=0。

    ★ 这条性质在挂起改造后更要紧：收尾事件的**序号**要在落库前盖好（归档按
      thread_seq 落，它是主键的一半），而**发布**要在落库之后 —— 分配与发布
      因此被拆成两步（relay.next_thread_seq + relay.emit）。拆错任何一边，
      要么归档撞主键，要么这个断言红。
    """
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]

    saw_terminal = False
    async for event in iter_stream(tid, after_seq=0, seconds=10.0):
        if event.depth == 0 and event.type in {"run.finished", "run.failed", "run.cancelled"}:
            saw_terminal = True
            break
    assert saw_terminal, "没等到终止事件"

    # 不做任何等待，立刻回查
    run = (await client.get(f"/v1/runs/{rid}")).json()
    assert run["status"] == "succeeded", run
    assert run["last_seq"] > 0


# ──────────────────────────────────────────────── 游标与限量


async def test_resume_with_last_event_id_skips_delivered_events(client) -> None:
    tid = await setup_thread(client)
    await run_once(client, tid)

    first, _ = await read_stream(tid, after_seq=None)
    assert len(first) >= 2

    cut = first[1].thread_seq
    resumed, _ = await read_stream(tid, after_seq=cut)

    assert resumed, "重连后应补发剩余事件"
    assert all(e.thread_seq > cut for e in resumed), [e.thread_seq for e in resumed]


async def test_first_connect_is_capped(clean_db: None) -> None:
    """★ 首连只回放最近 N 条 —— 否则打开老会话会把前端灌死。

    这是 thread 流相对 run 流**新增**的风险：run 流天然只有一轮的量，而一条
    会话可以横跨几百轮。
    """
    capped = get_settings().model_copy(update={"sse_thread_replay_events": 3})
    app = create_app()
    model = text_model("好")
    app.state.executor = InProcessExecutor(
        get_sessionmaker(), capped, model_builder=lambda *_a, **_k: model
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        tid = await setup_thread(c)
        for _ in range(3):
            await run_once(c, tid)

        full, _ = await read_stream(tid, after_seq=0)
        capped_events, _ = await read_stream(tid, after_seq=None, settings=capped)

    # 三轮 run 产出的事件远多于 3 条 —— 首连被窗口截断，显式 after_seq=0 不受限
    assert len(full) > 3, f"三轮应当产出多于 3 条归档事件，实际 {len(full)}"
    assert 0 < len(capped_events) <= 4, f"首连回放没有被限量：{len(capped_events)} 条"
    assert len(capped_events) < len(full)


async def test_explicit_after_seq_zero_means_from_the_beginning(client) -> None:
    """★ after_seq=0 与「没有游标」是**不同**的语义。

    0 = 从头给我；None = 给我默认窗口。用 0 当缺省值会让每次打开老会话都
    全量回放 —— 那正是限量要防的事。
    """
    tid = await setup_thread(client)
    await run_once(client, tid)

    events, _ = await read_stream(tid, after_seq=0)

    assert events
    assert events[0].thread_seq == 1, "显式 after_seq=0 应当从第一条开始"


# ──────────────────────────────────────────────── 归档接力


async def test_replay_falls_back_to_the_archive_when_redis_is_gone(client) -> None:
    """★ Redis 的流有 maxlen 裁剪，而会话流可以很长 —— 早期事件只在 Postgres。

    所以回放是「先归档、再 Redis」接力。Redis 整个没了也要能读出历史。
    """
    from atlas_server.redisx import make_redis
    from atlas_server.stream.relay import thread_stream_key

    tid = await setup_thread(client)
    await run_once(client, tid)

    redis = make_redis(get_settings())
    try:
        await redis.delete(thread_stream_key(UUID(tid)))
    finally:
        await redis.aclose()

    events, _ = await read_stream(tid, after_seq=0)

    types = [e.type for e in events]
    assert "run.started" in types, "Redis 没了之后应从归档回放"
    assert "run.finished" in types


# ──────────────────────────────────────────────── 并发闸门


def test_the_gate_refuses_beyond_the_limit() -> None:
    gate = StreamGate(2)
    with gate, gate:
        assert gate.active == 2
        with pytest.raises(StreamsBusy), gate:
            pass
    assert gate.active == 0


async def test_the_gate_releases_when_the_stream_ends() -> None:
    """★ 占用必须随**迭代**结束而释放，不是随路由函数返回。

    路由在 StreamingResponse 构造完就返回了，而流还要跑几小时 —— 在路由里
    占用的话计数会在第一帧之前就归零，闸门形同虚设。
    """
    gate = StreamGate(1)

    async def gen():
        yield "a"
        yield "b"

    wrapped = gate.guard(gen())
    assert gate.active == 0, "包装时还不该占用"
    got = [item async for item in wrapped]
    assert got == ["a", "b"]
    assert gate.active == 0, "迭代结束应释放"


async def test_the_gate_releases_on_client_disconnect() -> None:
    """客户端中途断开（生成器被 aclose）同样要释放，否则槽位泄漏。"""
    gate = StreamGate(1)

    async def gen():
        yield "a"
        yield "b"

    wrapped = gate.guard(gen())
    it = wrapped.__aiter__()
    assert await it.__anext__() == "a"
    assert gate.active == 1
    await wrapped.aclose()
    assert gate.active == 0, "断开后槽位没释放 —— 会一路泄漏到 503"
