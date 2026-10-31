"""会话与消息 API（文档 §11.2）。

POST /threads/{id}/runs 属于 P3（需要 RunExecutor），本轮不覆盖；
消息由测试直接写库来验证读取与分页。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_agent.db.models import Message
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.main import create_app
from atlas_agent.repositories.thread import ThreadRepository
from httpx import ASGITransport
from sqlalchemy import select

from tests.fake_uc import AS_USER


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def make_agent(client: httpx.AsyncClient, slug: str = "analyst") -> str:
    body: dict[str, Any] = {
        "slug": slug,
        "name": "数据分析师",
        "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
    }
    r = await client.post("/v1/agents", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def seed_messages(thread_id: str, n: int) -> None:
    """直接写库 —— P3 之前没有产生消息的 API。"""
    async with get_sessionmaker()() as session:
        repo = ThreadRepository(session)
        for i in range(n):
            await repo.add_message(
                thread_id=UUID(thread_id),
                role="user" if i % 2 == 0 else "assistant",
                content=[{"type": "text", "text": f"消息 {i}"}],
            )
        await session.commit()


# ---------------------------------------------------------------------------
# 创建 / 读取
# ---------------------------------------------------------------------------


async def test_create_thread(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    r = await client.post("/v1/threads", json={"agent_id": aid})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_slug"] == "analyst"
    assert body["status"] == "active"
    assert body["title_source"] == "pending"  # 待自动生成（决策 5）
    assert body["message_count"] == 0


async def test_created_thread_is_committed_before_the_response_is_sent(clean_db: None) -> None:
    """写后立读：客户端拿到 201 时会话已经提交，紧接着发消息不会报「会话不存在」。

    ★ 在测试里连发两个请求复现不了：ASGITransport 要等整个 ASGI 调用结束（含依赖的收尾）
      才把响应交给客户端。真实部署里响应先到、提交后到 —— 所以在应用发出响应体的那一刻查库。
    """
    app = create_app()
    committed: list[bool] = []

    async def probe(scope: Any, receive: Any, send: Any) -> None:
        async def send_and_check(message: Any) -> None:
            creating = scope["method"] == "POST" and scope["path"] == "/v1/threads"
            if creating and message["type"] == "http.response.body":
                tid = UUID(json.loads(message["body"])["id"])
                async with get_sessionmaker()() as session:
                    committed.append(await ThreadRepository(session).get(tid) is not None)
            await send(message)

        await app(scope, receive, send_and_check)

    transport = ASGITransport(app=probe)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        aid = await make_agent(c)
        r = await c.post("/v1/threads", json={"agent_id": aid})
    assert r.status_code == 201, r.text
    assert committed == [True]


async def test_tool_output_with_nul_is_stored_with_a_replacement_character(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    """命令输出里带二进制（NUL）：PG 的 jsonb 收不下，以前整条写不进去、整轮失败。"""
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    output = "PK\x03\x04\x00\x00binary\x00tail"
    async with get_sessionmaker()() as session:
        await ThreadRepository(session).add_message(
            thread_id=UUID(tid),
            role="user",
            kind="tool_result",
            content=[{"type": "tool_result", "tool_use_id": "call_1", "content": output}],
        )
        await session.commit()

    # 消息列表接口不返回工具结果（kind=tool_result），直接读库
    async with get_sessionmaker()() as session:
        [content] = (
            await session.scalars(select(Message.content).where(Message.thread_id == UUID(tid)))
        ).all()
    assert content[0]["content"] == output.replace("\x00", "\ufffd")


async def test_create_thread_with_title_marks_manual(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    aid = await make_agent(client)
    r = await client.post("/v1/threads", json={"agent_id": aid, "title": "我起的标题"})
    assert r.json()["title_source"] == "manual"


async def test_create_thread_with_unknown_agent_404(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    r = await client.post("/v1/threads", json={"agent_id": "00000000-0000-0000-0000-0000000000ff"})
    assert r.status_code == 404
    assert r.json()["error"]["kind"] == "not_found"


async def test_get_and_list_threads(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]

    assert (await client.get(f"/v1/threads/{tid}")).status_code == 200
    listed = (await client.get("/v1/threads")).json()
    assert [t["id"] for t in listed["data"]] == [tid]
    assert listed["next_cursor"] is None


# ---------------------------------------------------------------------------
# ★ 标题：用户改过就永不被自动生成覆盖（决策 5）
# ---------------------------------------------------------------------------


async def test_rename_sets_title_source_manual(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    assert (await client.get(f"/v1/threads/{tid}")).json()["title_source"] == "pending"

    r = await client.patch(f"/v1/threads/{tid}", json={"title": "Q2 留存归因"})
    assert r.status_code == 200
    assert r.json()["title"] == "Q2 留存归因"
    assert r.json()["title_source"] == "manual"


async def test_archive_thread(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    r = await client.patch(f"/v1/threads/{tid}", json={"status": "archived"})
    assert r.json()["status"] == "archived"

    active = (await client.get("/v1/threads", params={"status": "active"})).json()
    assert active["data"] == []


async def test_delete_thread(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    assert (await client.delete(f"/v1/threads/{tid}")).status_code == 204
    assert (await client.get(f"/v1/threads/{tid}")).status_code == 404


# ---------------------------------------------------------------------------
# 消息与游标分页
# ---------------------------------------------------------------------------


async def test_messages_are_newest_first(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    await seed_messages(tid, 5)

    body = (await client.get(f"/v1/threads/{tid}/messages")).json()
    texts = [m["content"][0]["text"] for m in body["data"]]
    assert texts == ["消息 4", "消息 3", "消息 2", "消息 1", "消息 0"]
    assert body["next_cursor"] is None


async def test_message_cursor_pagination_has_no_gaps_or_dupes(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    await seed_messages(tid, 12)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # 防御性上限，正常 3 页
        params = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get(f"/v1/threads/{tid}/messages", params=params)).json()
        seen += [m["id"] for m in page["data"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == 12
    assert len(set(seen)) == 12  # 无重复


async def test_bad_cursor_is_400_not_500(client: httpx.AsyncClient, clean_db: None) -> None:
    aid = await make_agent(client)
    tid = (await client.post("/v1/threads", json={"agent_id": aid})).json()["id"]
    r = await client.get(f"/v1/threads/{tid}/messages", params={"cursor": "!!!not-base64"})
    assert r.status_code == 400
    assert r.json()["error"]["kind"] == "invalid_cursor"


async def test_messages_of_missing_thread_404(client: httpx.AsyncClient, clean_db: None) -> None:
    r = await client.get("/v1/threads/00000000-0000-0000-0000-0000000000ff/messages")
    assert r.status_code == 404
