"""★ P3 完成标准（文档 §16）：对话能流式输出；中途刷新页面能续上。

模型经 executor 的 model_builder 注入假实现 —— 整条
POST /runs → 后台执行 → Redis Stream → SSE 的链路脱离网络运行。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.db.session import get_sessionmaker
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport

from tests.fakes import ScriptedChatModel, text_model, usage_model


def make_app(chunks: list[str] | None = None, delay: float = 0.0):
    """构造 app 并塞入注入了假模型的执行器。

    lifespan 在 ASGITransport 下不会自动运行，所以这里手动装配 executor。
    """
    app = create_app()
    model: ScriptedChatModel
    if chunks is None:
        model = usage_model(
            "你好，世界",
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    else:
        model = text_model("".join(chunks), pieces=len(chunks))
    model.delay = delay
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: model,
    )
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def setup_thread(client: httpx.AsyncClient) -> str:
    agent = await client.post(
        "/v1/agents",
        json={
            "slug": "streamer",
            "name": "流式测试",
            "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
        },
    )
    assert agent.status_code == 201, agent.text
    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    return thread.json()["id"]


async def wait_for_status(client: httpx.AsyncClient, run_id: str, *, timeout: float = 10.0) -> dict:
    """轮询到 run 进入终止态。"""
    deadline = asyncio.get_running_loop().time() + timeout
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 超时未结束")


def parse_sse(raw: str) -> list[tuple[int, str]]:
    """从 SSE 文本里抽出 (seq, event_type)。"""
    out: list[tuple[int, str]] = []
    seq: int | None = None
    for line in raw.splitlines():
        if line.startswith("id: "):
            seq = int(line[4:])
        elif line.startswith("event: ") and seq is not None:
            out.append((seq, line[7:]))
            seq = None
    return out


# ---------------------------------------------------------------------------
# 主路径
# ---------------------------------------------------------------------------


async def test_create_run_returns_202_immediately(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    tid = await setup_thread(client)
    r = await client.post(
        f"/v1/threads/{tid}/runs",
        json={"content": [{"type": "text", "text": "你好"}]},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    await wait_for_status(client, body["run_id"])


async def test_run_persists_assistant_message_and_usage(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]

    run = await wait_for_status(client, rid)
    assert run["status"] == "succeeded", run
    assert run["total_tokens"] == 15
    assert run["last_seq"] > 0

    msgs = (await client.get(f"/v1/threads/{tid}/messages")).json()["data"]
    roles = [m["role"] for m in msgs]
    assert roles == ["assistant", "user"]  # 倒序
    assert msgs[0]["content"][0]["text"] == "你好，世界"
    assert msgs[0]["run_id"] == rid

    thread = (await client.get(f"/v1/threads/{tid}")).json()
    assert thread["message_count"] == 2


# ---------------------------------------------------------------------------
# ★ SSE 与断线重连
# ---------------------------------------------------------------------------


async def test_sse_streams_events_in_order(client: httpx.AsyncClient, clean_db: None) -> None:
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    await wait_for_status(client, rid)

    resp = await client.get(f"/v1/runs/{rid}/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    seqs = [s for s, _ in events]
    types = [t for _, t in events]

    assert seqs == list(range(1, len(seqs) + 1))  # 契约：从 1 严格递增无空洞
    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert "message.delta" in types


async def test_resume_with_last_event_id_skips_delivered_events(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """★ 中途刷新页面能续上 —— 浏览器 EventSource 自动带 Last-Event-ID。"""
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    await wait_for_status(client, rid)

    full = parse_sse((await client.get(f"/v1/runs/{rid}/events")).text)
    assert len(full) >= 4

    resume_after = full[1][0]  # 假装只收到了前两条
    partial = parse_sse(
        (
            await client.get(f"/v1/runs/{rid}/events", headers={"Last-Event-ID": str(resume_after)})
        ).text
    )
    assert [s for s, _ in partial] == [s for s, _ in full if s > resume_after]
    assert partial[-1][1] == "run.finished"  # 仍能看到终止事件


async def test_replay_falls_back_to_postgres_when_redis_expired(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """Redis 24h TTL 过期后，历史会话仍能从 run_event 归档表回放（§10.2 情形 2）。"""
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    await wait_for_status(client, rid)

    from atlas_server.stream.relay import stream_key

    settings = get_settings()
    import redis.asyncio as aioredis

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(stream_key(__import__("uuid").UUID(rid)))  # 模拟 TTL 过期
    finally:
        await redis.aclose()

    events = parse_sse((await client.get(f"/v1/runs/{rid}/events")).text)
    assert events, "Redis 过期后应从 Postgres 归档回放"
    assert events[0][1] == "run.started"
    assert events[-1][1] == "run.finished"


# ---------------------------------------------------------------------------
# 串行锁 / 幂等 / 取消
# ---------------------------------------------------------------------------


async def test_second_run_on_same_thread_conflicts(clean_db: None) -> None:
    """同一会话串行：已有运行中的 run 时返回 409（§9）。"""
    transport = ASGITransport(app=make_app(chunks=["a"] * 6, delay=0.15))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tid = await setup_thread(client)
        first = await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "1"}]}
        )
        assert first.status_code == 202

        second = await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "2"}]}
        )
        assert second.status_code == 409
        assert second.json()["error"]["kind"] == "thread_locked"

        await wait_for_status(client, first.json()["run_id"])


async def test_idempotency_key_returns_same_run(client: httpx.AsyncClient, clean_db: None) -> None:
    tid = await setup_thread(client)
    headers = {"Idempotency-Key": "abc-123"}
    body = {"content": [{"type": "text", "text": "hi"}]}

    first = await client.post(f"/v1/threads/{tid}/runs", json=body, headers=headers)
    assert first.status_code == 202
    rid = first.json()["run_id"]
    await wait_for_status(client, rid)

    again = await client.post(f"/v1/threads/{tid}/runs", json=body, headers=headers)
    assert again.status_code == 202
    assert again.json()["run_id"] == rid  # 不该跑第二次

    msgs = (await client.get(f"/v1/threads/{tid}/messages")).json()["data"]
    assert len(msgs) == 2  # 只有一轮问答，没被重复提交撑成四条


async def test_cancel_marks_run_cancelled(clean_db: None) -> None:
    transport = ASGITransport(app=make_app(chunks=["x"] * 20, delay=0.1))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tid = await setup_thread(client)
        rid = (
            await client.post(
                f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
            )
        ).json()["run_id"]

        await asyncio.sleep(0.25)
        assert (await client.post(f"/v1/runs/{rid}/cancel")).status_code == 200

        run = await wait_for_status(client, rid)
        assert run["status"] == "cancelled"


async def test_cancel_finished_run_conflicts(client: httpx.AsyncClient, clean_db: None) -> None:
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    await wait_for_status(client, rid)
    r = await client.post(f"/v1/runs/{rid}/cancel")
    assert r.status_code == 409


async def test_run_and_events_404(client: httpx.AsyncClient, clean_db: None) -> None:
    missing = "00000000-0000-0000-0000-0000000000ff"
    assert (await client.get(f"/v1/runs/{missing}")).status_code == 404
    # SSE 路由也必须以 JSON 404 返回，而不是一个空的 200 流
    assert (await client.get(f"/v1/runs/{missing}/events")).status_code == 404


# ---------------------------------------------------------------------------
# ★ 孤儿回收（进程内执行的代价，§12.1 / R2）
# ---------------------------------------------------------------------------


async def test_reap_orphans_marks_interrupted(client: httpx.AsyncClient, clean_db: None) -> None:
    from sqlalchemy import text

    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    await wait_for_status(client, rid)

    # 伪造"上次进程被杀时还在跑"的状态
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE run SET status='running', finished_at=NULL WHERE id=CAST(:i AS uuid)"),
            {"i": rid},
        )
        await session.commit()

    import redis.asyncio as aioredis
    from atlas_server.services.run import RunService

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with get_sessionmaker()() as session:
            service = RunService(session, redis, settings, None)  # type: ignore[arg-type]
            assert await service.reap_orphans() >= 1
    finally:
        await redis.aclose()

    body = (await client.get(f"/v1/runs/{rid}")).json()
    assert body["status"] == "interrupted"
    assert body["error_kind"] == "interrupted"


# ---------------------------------------------------------------------------
# ★ 顺序保证：看到终止事件 ⇒ DB 已是最终状态
# ---------------------------------------------------------------------------


async def test_terminal_event_is_published_after_persist(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """前端收到 run.finished 会立刻回查最终用量。

    曾经的顺序是"先发布终止事件、后落库"，于是这一查读到 status=running、
    tokens=0、last_seq=0。终止事件必须是最后一步。
    """
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]

    saw_terminal = False
    async with client.stream("GET", f"/v1/runs/{rid}/events") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: ") and line[7:] in (
                "run.finished",
                "run.failed",
                "run.cancelled",
            ):
                saw_terminal = True
                break
    assert saw_terminal

    # 不做任何等待，立刻回查
    run = (await client.get(f"/v1/runs/{rid}")).json()
    assert run["status"] == "succeeded", run
    assert run["total_tokens"] == 15
    assert run["last_seq"] > 0


async def test_reconnect_after_finish_closes_immediately(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """★ run 跑完之后刷新页面：必须立刻关闭，不能挂住。

    曾经的行为是进 XREAD BLOCK 一直发心跳直到客户端超时 ——
    而"跑完后刷新"是前端最常见的动作之一。
    终止 run 的事件集完整且不可变，补发缺口后就该结束。
    """
    tid = await setup_thread(client)
    rid = (
        await client.post(
            f"/v1/threads/{tid}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
    ).json()["run_id"]
    run = await wait_for_status(client, rid)
    last_seq = run["last_seq"]

    # 游标停在最后一个事件之后 —— 没有任何新事件可给
    resp = await asyncio.wait_for(
        client.get(f"/v1/runs/{rid}/events", headers={"Last-Event-ID": str(last_seq)}),
        timeout=5.0,  # 挂住的话这里会 TimeoutError
    )
    assert resp.status_code == 200
    assert parse_sse(resp.text) == []

    # 从头拉仍然拿得到完整事件集
    full = parse_sse((await client.get(f"/v1/runs/{rid}/events")).text)
    assert full[-1][1] == "run.finished"


async def test_release_thread_lock_requires_owner() -> None:
    """★ 锁 owner 校验（评审 H3）：TTL 过期后被新 run 持有的锁，
    旧 run 收尾时不得误删 —— 否则「同一会话串行」的窗口被再撕开一次。"""
    from uuid import uuid4

    import redis.asyncio as aioredis
    from atlas_server.config import get_settings
    from atlas_server.stream.relay import EventRelay, thread_lock_key

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    relay = EventRelay(redis)
    tid, old_run, new_run = uuid4(), uuid4(), uuid4()
    try:
        assert await relay.acquire_thread_lock(tid, owner=old_run, ttl_s=30)
        assert not await relay.acquire_thread_lock(tid, owner=new_run, ttl_s=30)  # 串行仍成立

        await redis.delete(thread_lock_key(tid))  # 模拟 TTL 过期
        assert await relay.acquire_thread_lock(tid, owner=new_run, ttl_s=30)

        await relay.release_thread_lock(tid, owner=old_run)  # 旧 run 结束收尾
        assert await redis.exists(thread_lock_key(tid))  # 新 run 的锁必须还在

        await relay.release_thread_lock(tid, owner=new_run)
        assert not await redis.exists(thread_lock_key(tid))
    finally:
        await redis.delete(thread_lock_key(tid))
        await redis.aclose()
