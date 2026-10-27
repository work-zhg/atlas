"""记忆服务的规则（doc/memory-design.html §3）：经 HTTP 接口验证。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from memory_testkit import FakePermissions, add_scope

U = "/v1/scopes/user/instances/uid=u1"


def at(hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, 26, hour, minute, tzinfo=UTC).isoformat()


async def topic(c: httpx.AsyncClient, code: str, *, base: str = U, **attrs: Any) -> dict:
    r = await c.post(
        f"{base}/topics", json={"code": code, "description": f"{code} 的说明", "attrs": attrs}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def write(c: httpx.AsyncClient, *, base: str = U, **body: Any) -> dict:
    r = await c.post(f"{base}/writes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def load(c: httpx.AsyncClient, code: str, *, base: str = U) -> dict:
    r = await c.get(f"{base}/topics/{code}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def user_scope() -> None:
    await add_scope("user", ["uid"], description="用户")


# ───────────────────────────── 主题与写入


async def test_empty_catalog_for_unknown_instance(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    r = await client.get(f"{U}/topics")
    assert r.status_code == 200
    assert r.json()["topics"] == []
    assert r.json()["version"] == 0


async def test_write_add_reports_size_and_version(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    t = await topic(client, "沟通偏好", resident=True)
    assert t["attrs"] == {"resident": True}
    first = await write(
        client, topic="沟通偏好", content="回复使用中文", occurred_at=at(10), source="agent"
    )
    assert first["outcome"] == "applied"
    assert first["added"]["number"] == 1
    [change] = first["changed"]
    assert change["topic"] == "沟通偏好"
    assert change["version_before"] == t["version"]
    assert change["version_after"] > t["version"]

    second = await write(
        client, topic="沟通偏好", content="技术术语保留英文", occurred_at=at(10, 5)
    )
    assert second["added"]["number"] == 2
    assert second["changed"][0]["version_before"] == change["version_after"]

    loaded = await load(client, "沟通偏好")
    assert [e["number"] for e in loaded["entries"]] == [1, 2]
    assert loaded["topic"]["entry_count"] == 2
    assert loaded["topic"]["char_count"] == len("回复使用中文") + len("技术术语保留英文")
    assert loaded["topic"]["version"] == second["changed"][0]["version_after"]


async def test_supersede_in_one_write(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "项目约定")
    await write(client, topic="项目约定", content="测试命令是 pytest", occurred_at=at(9))
    r = await write(
        client, topic="项目约定", content="测试命令是 make test", remove=[1], occurred_at=at(11)
    )
    assert r["removed"] == [1]
    assert r["added"]["number"] == 2
    loaded = await load(client, "项目约定")
    assert [e["content"] for e in loaded["entries"]] == ["测试命令是 make test"]

    detail = (await client.get(f"{U}/entries/1")).json()
    assert detail["entry"]["status"] == "deleted"
    assert detail["entry"]["superseded_by"] == 2
    assert [e["number"] for e in detail["chain"]] == [1, 2]


async def test_later_write_wins_design_example(client: httpx.AsyncClient, user_scope: None) -> None:
    """§6.4 的例子：11:00 的写入已取代 #1；11:30 到达、时间是 10:00 的补记被丢弃。"""
    await topic(client, "项目约定")
    await write(client, topic="项目约定", content="测试命令是 pytest", occurred_at=at(9))
    await write(
        client, topic="项目约定", content="测试命令是 make test", remove=[1], occurred_at=at(11)
    )
    late = await write(
        client,
        topic="项目约定",
        content="测试命令是 tox",
        remove=[1],
        occurred_at=at(10),
        source="backfill",
    )
    assert late["outcome"] == "discarded"
    assert late["conflicts"] == [{"number": 1, "current": 2, "current_at": at(11)}]
    loaded = await load(client, "项目约定")
    assert [e["content"] for e in loaded["entries"]] == ["测试命令是 make test"]


async def test_newer_write_follows_supersede_chain(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 pnpm", occurred_at=at(9))
    await write(client, topic="工具", content="改用 bun", remove=[1], occurred_at=at(10))
    # 写入方只看到过 #1，但它的时间更晚：沿取代关系找到 #2 并取代它
    r = await write(client, topic="工具", content="改用 yarn", remove=[1], occurred_at=at(12))
    assert r["outcome"] == "applied"
    assert r["removed"] == [2]
    loaded = await load(client, "工具")
    assert [e["content"] for e in loaded["entries"]] == ["改用 yarn"]


async def test_active_entry_newer_than_write_is_kept(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 bun", occurred_at=at(11))
    r = await write(client, topic="工具", content="用 npm", remove=[1], occurred_at=at(10))
    assert r["outcome"] == "discarded"


async def test_forget_then_writes(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 pnpm", occurred_at=at(9))
    forget = await write(client, remove=[1], occurred_at=at(11))
    assert forget["removed"] == [1] and forget["added"] is None
    # 遗忘更晚：丢弃
    stale = await write(client, topic="工具", content="用 npm", remove=[1], occurred_at=at(10))
    assert stale["outcome"] == "discarded"
    assert stale["conflicts"][0]["current"] is None
    # 遗忘更早：只做新增
    newer = await write(client, topic="工具", content="用 bun", remove=[1], occurred_at=at(12))
    assert newer["outcome"] == "applied"
    assert newer["removed"] == [] and newer["skipped"] == [1]
    assert newer["added"]["content"] == "用 bun"


async def test_write_validation(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    r = await client.post(f"{U}/writes", json={"occurred_at": at(9)})
    assert r.status_code == 400
    r = await client.post(
        f"{U}/writes", json={"topic": "工具", "content": "x", "occurred_at": "2026-10-26T10:00:00"}
    )
    assert r.status_code == 400  # 时间不带时区
    r = await client.post(f"{U}/writes", json={"remove": [99], "occurred_at": at(9)})
    assert r.status_code == 404
    assert r.json()["code"] == "ENTRY_NOT_FOUND"
    r = await client.post(
        f"{U}/writes", json={"topic": "不存在", "content": "x", "occurred_at": at(9)}
    )
    assert r.status_code == 404
    assert r.json()["code"] == "TOPIC_NOT_FOUND"
    assert [t["code"] for t in r.json()["context"]["catalog"]] == ["工具"]


async def test_idempotent_write(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    body = {"topic": "工具", "content": "用 bun", "occurred_at": at(9), "idempotency_key": "k1"}
    first = await write(client, **body)
    again = await write(client, **body)
    assert again["replayed"] is True
    assert again["added"]["number"] == first["added"]["number"]
    assert len((await load(client, "工具"))["entries"]) == 1


async def test_topic_code_unique_among_active(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    r = await client.post(f"{U}/topics", json={"code": "工具"})
    assert r.status_code == 409
    assert r.json()["code"] == "TOPIC_EXISTS"
    r = await client.post(f"{U}/topics", json={"code": "a/b"})
    assert r.status_code == 400


async def test_update_topic_bumps_version(client: httpx.AsyncClient, user_scope: None) -> None:
    t = await topic(client, "工具")
    r = await client.patch(f"{U}/topics/工具", json={"attrs": {"resident": True}})
    assert r.status_code == 200
    assert r.json()["topic"]["attrs"] == {"resident": True}
    assert r.json()["changed"][0]["version_before"] == t["version"]
    assert r.json()["topic"]["version"] > t["version"]


async def test_move_entries(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "编码")
    await topic(client, "编码-java")
    await write(client, topic="编码", content="不使用 Lombok", occurred_at=at(9))
    await write(client, topic="编码", content="缩进用 2 空格", occurred_at=at(9))
    r = await client.post(f"{U}/moves", json={"entries": [1], "to_topic": "编码-java"})
    assert r.status_code == 200
    assert {c["topic"] for c in r.json()["changed"]} == {"编码", "编码-java"}
    assert [e["number"] for e in (await load(client, "编码-java"))["entries"]] == [1]
    assert (await load(client, "编码"))["topic"]["entry_count"] == 1


async def test_delete_topic_only_when_empty(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 bun", occurred_at=at(9))
    assert (await client.delete(f"{U}/topics/工具")).status_code == 409
    await write(client, remove=[1], occurred_at=at(10))
    assert (await client.delete(f"{U}/topics/工具")).status_code == 204
    r = await client.get(f"{U}/topics/工具")
    assert r.status_code == 409
    assert r.json()["code"] == "TOPIC_GONE"


# ───────────────────────────── 拆分


async def _filled_coding(client: httpx.AsyncClient) -> int:
    await topic(client, "编码", resident=True)
    await write(client, topic="编码", content="Java 偏好构造器注入", occurred_at=at(9))
    await write(client, topic="编码", content="JS 项目用 bun", occurred_at=at(9))
    await write(client, topic="编码", content="Java 不使用 Lombok", occurred_at=at(9))
    return int((await load(client, "编码"))["topic"]["version"])


async def test_split_moves_entries_atomically(client: httpx.AsyncClient, user_scope: None) -> None:
    version = await _filled_coding(client)
    r = await client.post(
        f"{U}/topics/编码/split",
        json={
            "expected_version": version,
            "groups": [
                {
                    "code": "编码-java",
                    "description": "Java",
                    "attrs": {"resident": True},
                    "entries": [1, 3],
                },
                {
                    "code": "编码-js",
                    "description": "JS",
                    "attrs": {"resident": True},
                    "entries": [2],
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    catalog = (await client.get(f"{U}/topics")).json()["topics"]
    assert [t["code"] for t in catalog] == ["编码-java", "编码-js"]
    java = await load(client, "编码-java")
    assert [e["number"] for e in java["entries"]] == [1, 3]
    assert java["topic"]["attrs"] == {"resident": True}
    # 写进已拆掉的主题：拒绝，并告诉客户端拆成了哪些、当前目录是什么
    r = await client.post(
        f"{U}/writes", json={"topic": "编码", "content": "x", "occurred_at": at(10)}
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "TOPIC_GONE"
    assert body["context"]["split_into"] == ["编码-java", "编码-js"]
    assert [t["code"] for t in body["context"]["catalog"]] == ["编码-java", "编码-js"]
    # 按编号删除照样有效
    gone = await write(client, remove=[2], occurred_at=at(10))
    assert gone["changed"][0]["topic"] == "编码-js"


async def test_split_rejected_when_topic_changed(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    version = await _filled_coding(client)
    await write(client, topic="编码", content="Java 用 JUnit 5", occurred_at=at(10))
    r = await client.post(
        f"{U}/topics/编码/split",
        json={
            "expected_version": version,
            "groups": [
                {"code": "a", "entries": [1, 3]},
                {"code": "b", "entries": [2]},
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["code"] == "VERSION_MISMATCH"


async def test_split_must_cover_all_entries(client: httpx.AsyncClient, user_scope: None) -> None:
    version = await _filled_coding(client)
    r = await client.post(
        f"{U}/topics/编码/split",
        json={
            "expected_version": version,
            "groups": [{"code": "a", "entries": [1]}, {"code": "b", "entries": [2]}],
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "SPLIT_COVERAGE"
    assert r.json()["context"]["missing"] == [3]


async def test_split_may_reuse_original_code(client: httpx.AsyncClient, user_scope: None) -> None:
    version = await _filled_coding(client)
    r = await client.post(
        f"{U}/topics/编码/split",
        json={
            "expected_version": version,
            "groups": [{"code": "编码", "entries": [1, 3]}, {"code": "编码-js", "entries": [2]}],
        },
    )
    assert r.status_code == 200, r.text
    assert [e["number"] for e in (await load(client, "编码"))["entries"]] == [1, 3]


# ───────────────────────────── 撤销、恢复、彻底删除


async def test_undo_supersede(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 pnpm", occurred_at=at(9))
    w = await write(client, topic="工具", content="改用 bun", remove=[1], occurred_at=at(10))
    r = await client.post(f"{U}/revisions/{w['revision']}/undo", json={"source": "user"})
    assert r.status_code == 200, r.text
    assert r.json()["revision"]["kind"] == "undo"
    assert [e["content"] for e in (await load(client, "工具"))["entries"]] == ["用 pnpm"]
    again = await client.post(f"{U}/revisions/{w['revision']}/undo")
    assert again.status_code == 409
    assert again.json()["code"] == "ALREADY_UNDONE"


async def test_undo_refused_after_later_change(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    w = await write(client, topic="工具", content="用 pnpm", occurred_at=at(9))
    await write(client, topic="工具", content="改用 bun", remove=[1], occurred_at=at(10))
    r = await client.post(f"{U}/revisions/{w['revision']}/undo")
    assert r.status_code == 409
    assert r.json()["code"] == "UNDO_STALE"


async def test_restore_into_split_child(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "编码")
    await write(client, topic="编码", content="Java 用 Lombok", occurred_at=at(9))
    await write(client, remove=[1], occurred_at=at(9, 30))
    await write(client, topic="编码", content="Java 偏好构造器注入", occurred_at=at(10))
    await write(client, topic="编码", content="JS 项目用 bun", occurred_at=at(10))
    version = (await load(client, "编码"))["topic"]["version"]
    r = await client.post(
        f"{U}/topics/编码/split",
        json={
            "expected_version": version,
            "groups": [{"code": "编码-java", "entries": [2]}, {"code": "编码-js", "entries": [3]}],
        },
    )
    assert r.status_code == 200
    r = await client.post(f"{U}/entries/1/restore")
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["topic"] == "编码-java"
    assert [e["number"] for e in (await load(client, "编码-java"))["entries"]] == [1, 2]


async def test_purge_removes_whole_chain_and_history(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    await topic(client, "工具")
    await write(client, topic="工具", content="用 pnpm", occurred_at=at(9))
    await write(client, topic="工具", content="改用 bun", remove=[1], occurred_at=at(10))
    await write(client, topic="工具", content="缩进 2 空格", occurred_at=at(10))
    r = await client.delete(f"{U}/entries/2")
    assert r.status_code == 200, r.text
    assert r.json()["purged"] == [1, 2]
    entries = (await client.get(f"{U}/entries", params={"status": "all"})).json()["data"]
    assert [e["number"] for e in entries] == [3]
    revisions = (await client.get(f"{U}/revisions")).json()["data"]
    assert [rev["added"] for rev in revisions] == [[3]]


async def test_list_entries_filters(client: httpx.AsyncClient, user_scope: None) -> None:
    await topic(client, "工具")
    await write(
        client,
        topic="工具",
        content="用 bun",
        occurred_at=at(9),
        source="agent",
        origin={"thread": "t1"},
    )
    await write(
        client,
        topic="工具",
        content="用 make",
        occurred_at=at(9),
        source="backfill",
        origin={"thread": "t2"},
    )
    r = await client.get(f"{U}/entries", params={"source": "backfill"})
    assert [e["content"] for e in r.json()["data"]] == ["用 make"]
    r = await client.get(f"{U}/entries", params={"origin": '{"thread": "t1"}'})
    assert [e["content"] for e in r.json()["data"]] == ["用 bun"]
    r = await client.get(f"{U}/revisions", params={"origin": '{"thread": "t2"}'})
    assert len(r.json()["data"]) == 1


# ───────────────────────────── 并发


async def test_concurrent_writes_are_serialized(
    client: httpx.AsyncClient, user_scope: None
) -> None:
    await topic(client, "工具")
    bodies = [{"topic": "工具", "content": f"事实 {i}", "occurred_at": at(9)} for i in range(8)]
    results = await asyncio.gather(*(client.post(f"{U}/writes", json=b) for b in bodies))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    numbers = sorted(r.json()["added"]["number"] for r in results)
    assert numbers == list(range(1, 9))
    versions = [r.json()["changed"][0]["version_after"] for r in results]
    assert len(set(versions)) == 8
    assert (await load(client, "工具"))["topic"]["entry_count"] == 8


# ───────────────────────────── 关联主键


async def test_composite_key_and_partial_listing(client: httpx.AsyncClient) -> None:
    await add_scope("user_project", ["uid", "project"])
    a = "/v1/scopes/user_project/instances/uid=u1,project=atlas"
    # 字段顺序不影响定位
    b = "/v1/scopes/user_project/instances/project=atlas,uid=u1"
    await topic(client, "项目约定", base=a)
    assert [t["code"] for t in (await client.get(f"{b}/topics")).json()["topics"]] == ["项目约定"]
    await topic(client, "项目约定", base="/v1/scopes/user_project/instances/uid=u1,project=other")
    await topic(client, "项目约定", base="/v1/scopes/user_project/instances/uid=u2,project=atlas")
    r = await client.get("/v1/scopes/user_project/instances", params={"uid": "u1"})
    assert sorted(i["key"]["project"] for i in r.json()["data"]) == ["atlas", "other"]


async def test_bad_key(client: httpx.AsyncClient, user_scope: None) -> None:
    r = await client.get("/v1/scopes/user/instances/project=x/topics")
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_KEY"
    r = await client.get("/v1/scopes/nope/instances/uid=u1/topics")
    assert r.status_code == 404
    assert r.json()["code"] == "SCOPE_NOT_FOUND"


# ───────────────────────────── 数据权限（§3.7）


@pytest.fixture
async def guarded_scope() -> None:
    await add_scope("user", ["uid"], description="用户", check_permission=True)


def as_user(uid: str) -> dict[str, str]:
    return {"X-Uid": uid, "X-Caller-App": "atlas"}


async def test_unregistered_instance_rejected(
    client: httpx.AsyncClient, guarded_scope: None
) -> None:
    r = await client.get(f"{U}/topics")
    assert r.status_code == 401
    r = await client.get(f"{U}/topics", headers=as_user("u1"))
    assert r.status_code == 403
    assert r.json()["code"] == "NOT_REGISTERED"


async def test_register_then_owner_only(
    client: httpx.AsyncClient, guarded_scope: None, perms: FakePermissions
) -> None:
    r = await client.post(f"{U}/register", json={"owner_uid": "u1"})
    assert r.status_code == 200, r.text
    assert r.json()["owner_written"] is True
    instance_id = r.json()["id"]
    assert perms.grants[("MemoryUser", instance_id)] == {"u1": "OWNER"}
    # 再登记（哪怕给了别的 Owner）也不改动
    again = await client.post(f"{U}/register", json={"owner_uid": "u2"})
    assert again.json()["owner_written"] is False
    assert perms.grants[("MemoryUser", instance_id)] == {"u1": "OWNER"}

    h = as_user("u1")
    assert (await client.post(f"{U}/topics", json={"code": "工具"}, headers=h)).status_code == 201
    other = as_user("u2")
    r = await client.get(f"{U}/topics", headers=other)
    assert r.status_code == 403
    assert r.json()["code"] == "NO_PERMISSION"
    # X-Auth-Uid 优先于 X-Uid
    r = await client.get(f"{U}/topics", headers={**other, "X-Auth-Uid": "u1"})
    assert r.status_code == 200


async def test_levels(
    client: httpx.AsyncClient, guarded_scope: None, perms: FakePermissions
) -> None:
    instance_id = (await client.post(f"{U}/register", json={"owner_uid": "u1"})).json()["id"]
    await client.post(f"{U}/topics", json={"code": "工具"}, headers=as_user("u1"))
    await client.post(
        f"{U}/writes",
        json={"topic": "工具", "content": "用 bun", "occurred_at": at(9)},
        headers=as_user("u1"),
    )
    perms.grants[("MemoryUser", instance_id)]["reader"] = "READ"
    perms.grants[("MemoryUser", instance_id)]["writer"] = "WRITE"
    assert (await client.get(f"{U}/topics/工具", headers=as_user("reader"))).status_code == 200
    r = await client.post(
        f"{U}/writes",
        json={"topic": "工具", "content": "x", "occurred_at": at(9)},
        headers=as_user("reader"),
    )
    assert r.status_code == 403
    assert (await client.delete(f"{U}/entries/1", headers=as_user("writer"))).status_code == 403
    assert (await client.delete(f"{U}/entries/1", headers=as_user("u1"))).status_code == 200


async def test_uc_down_rejects(
    client: httpx.AsyncClient, guarded_scope: None, perms: FakePermissions
) -> None:
    await client.post(f"{U}/register", json={"owner_uid": "u1"})
    perms.down = True
    r = await client.get(f"{U}/topics", headers=as_user("u1"))
    assert r.status_code == 503


async def test_purge_instance_cleans_uc(
    client: httpx.AsyncClient, guarded_scope: None, perms: FakePermissions
) -> None:
    instance_id = (await client.post(f"{U}/register", json={"owner_uid": "u1"})).json()["id"]
    await client.post(f"{U}/topics", json={"code": "工具"}, headers=as_user("u1"))
    r = await client.delete(U, headers=as_user("u1"))
    assert r.status_code == 204
    assert perms.cleaned == [("MemoryUser", instance_id)]
    # 实例没了：再访问就是「未登记」
    r = await client.get(f"{U}/topics", headers=as_user("u1"))
    assert r.json()["code"] == "NOT_REGISTERED"


async def test_list_instances_filtered_by_permission(
    client: httpx.AsyncClient, perms: FakePermissions
) -> None:
    await add_scope("user_project", ["uid", "project"], check_permission=True)
    base = "/v1/scopes/user_project/instances"
    await client.post(f"{base}/uid=u1,project=a/register", json={"owner_uid": "u1"})
    await client.post(f"{base}/uid=u1,project=b/register", json={"owner_uid": "someone"})
    r = await client.get(base, params={"uid": "u1"}, headers=as_user("u1"))
    assert [i["key"]["project"] for i in r.json()["data"]] == ["a"]


# ───────────────────────────── 作用域管理


async def test_scope_admin_requires_operation(client: httpx.AsyncClient) -> None:
    body = {"code": "team", "key_fields": ["team"], "check_permission": True}
    r = await client.post("/v1/admin/scopes", json=body, headers=as_user("bob"))
    assert r.status_code == 403
    r = await client.post("/v1/admin/scopes", json=body, headers=as_user("admin"))
    assert r.status_code == 201, r.text
    assert r.json()["data_code"] == "MemoryTeam"
    r = await client.get("/v1/scopes")
    assert [s["code"] for s in r.json()["data"]] == ["team"]
    r = await client.patch(
        "/v1/admin/scopes/team", json={"description": "团队"}, headers=as_user("admin")
    )
    assert r.json()["description"] == "团队"
