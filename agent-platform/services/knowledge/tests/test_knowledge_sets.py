"""知识集：谁能看到、谁能改、建库 / 移库、公有私有、内部接口按知识集给库。"""

from __future__ import annotations

from typing import Any

ALICE = {"X-Uid": "alice"}
BOB = {"X-Uid": "bob"}
CAROL = {"X-Uid": "carol"}


async def _set(client: Any, headers: dict[str, str] = ALICE, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/knowledge-sets",
        json={"name": "人事制度", "description": "考勤、请假、报销", **body},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _kb(client: Any, headers: dict[str, str] = ALICE, **body: Any) -> dict[str, Any]:
    r = await client.post(
        "/v1/datasets",
        json={"name": "考勤", "embedding_model": "fake-embedding", **body},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_set_visibility_follows_readable_datasets(client: Any) -> None:
    hr = await _set(client, top_k=6)
    assert hr["maintainers"] == [{"id": "alice", "name": "用户alice"}]
    assert hr["can_manage"] and hr["top_k"] == 6 and hr["dataset_count"] == 0
    # 空的知识集只有维护人看得到
    assert hr["id"] not in {
        s["id"] for s in (await client.get("/v1/knowledge-sets", headers=BOB)).json()["data"]
    }
    assert (await client.get(f"/v1/knowledge-sets/{hr['id']}", headers=BOB)).status_code == 404

    private = await _kb(client, set_id=hr["id"], name="薪酬")
    public = await _kb(client, set_id=hr["id"], name="考勤", visibility="public")
    assert private["set_id"] == hr["id"] and private["visibility"] == "private"
    assert public["visibility"] == "public"

    # bob 只读得到公有的那个：知识集对他可见，库数只算他能读的
    listed = (await client.get("/v1/knowledge-sets", headers=BOB)).json()["data"]
    mine = next(s for s in listed if s["id"] == hr["id"])
    assert mine["dataset_count"] == 1 and not mine["can_manage"]
    rows = (await client.get(f"/v1/datasets?set_id={hr['id']}", headers=BOB)).json()["data"]
    assert [d["id"] for d in rows] == [public["id"]]
    assert rows[0]["my_level"] == "READ"
    alice_rows = (await client.get(f"/v1/datasets?set_id={hr['id']}", headers=ALICE)).json()
    assert alice_rows["total"] == 2

    # 改成公有要 Owner；改完 bob 就读得到
    r = await client.patch(
        f"/v1/datasets/{private['id']}", json={"visibility": "public"}, headers=ALICE
    )
    assert r.status_code == 200 and r.json()["visibility"] == "public"
    assert (await client.get(f"/v1/datasets/{private['id']}", headers=BOB)).status_code == 200


async def test_only_maintainers_manage_a_set(client: Any, rt: Any) -> None:
    hr = await _set(client)
    await _kb(client, set_id=hr["id"], visibility="public")
    # bob 看得到，但不是维护人：不能改、不能往里建库
    r = await client.patch(f"/v1/knowledge-sets/{hr['id']}", json={"name": "x"}, headers=BOB)
    assert r.status_code == 403
    r = await client.post(
        "/v1/datasets",
        json={"name": "b", "embedding_model": "fake-embedding", "set_id": hr["id"]},
        headers=BOB,
    )
    assert r.status_code == 403

    # 维护人加上 bob 之后就可以
    r = await client.patch(
        f"/v1/knowledge-sets/{hr['id']}",
        json={"maintainers": ["alice", "bob"], "top_k": 8, "description": "新的介绍"},
        headers=ALICE,
    )
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["maintainers"]] == ["alice", "bob"]
    assert r.json()["top_k"] == 8
    await _kb(client, BOB, set_id=hr["id"], name="bob 的库")

    for bad in ({"maintainers": []}, {"top_k": 0}, {"name": " "}):
        r = await client.patch(f"/v1/knowledge-sets/{hr['id']}", json=bad, headers=ALICE)
        assert r.status_code == 400, bad

    # 「全部知识库 · Owner」能管任何知识集
    rt.perms.type_levels["carol"] = "OWNER"
    r = await client.patch(f"/v1/knowledge-sets/{hr['id']}", json={"name": "人事"}, headers=CAROL)
    assert r.status_code == 200 and r.json()["can_manage"]


async def test_move_and_delete(client: Any) -> None:
    hr = await _set(client)
    kb = await _kb(client, set_id=hr["id"])
    other = await _set(client, BOB, name="财务")

    # 移库：库的 Owner，且是目标知识集的维护人
    r = await client.patch(f"/v1/datasets/{kb['id']}", json={"set_id": other["id"]}, headers=ALICE)
    assert r.status_code == 404  # 财务是空的、alice 不是维护人：看不到
    await client.patch(
        f"/v1/knowledge-sets/{other['id']}", json={"maintainers": ["bob", "alice"]}, headers=BOB
    )
    # 非空的知识集不能删
    r = await client.delete(f"/v1/knowledge-sets/{hr['id']}", headers=ALICE)
    assert r.status_code == 409
    r = await client.patch(f"/v1/datasets/{kb['id']}", json={"set_id": other["id"]}, headers=ALICE)
    assert r.status_code == 200 and r.json()["set_id"] == other["id"]
    assert (await client.delete(f"/v1/knowledge-sets/{hr['id']}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/v1/knowledge-sets/{hr['id']}", headers=ALICE)).status_code == 404


async def test_dataset_without_set_gets_its_own(client: Any) -> None:
    """旧调用方不传 set_id：配一个同名、同 id 的知识集。"""
    kb = await _kb(client, name="老库")
    assert kb["set_id"] == kb["id"]
    got = (await client.get(f"/v1/knowledge-sets/{kb['id']}", headers=ALICE)).json()
    assert got["name"] == "老库" and got["dataset_count"] == 1

    # 旧的「全员可见」就是公有，不再给根部门授权
    team = await _kb(client, name="全员", permission="all_team")
    assert team["visibility"] == "public"
    acl = (await client.get(f"/v1/datasets/{team['id']}/grants", headers=ALICE)).json()["data"]
    assert [g["subject"]["type"] for g in acl] == ["USER"]


async def test_internal_readable_sets(client: Any) -> None:
    hr = await _set(client, top_k=5)
    public = await _kb(client, set_id=hr["id"], name="考勤", visibility="public")
    await _kb(client, set_id=hr["id"], name="薪酬")
    empty = await _set(client, name="空的")
    fin = await _set(client, name="财务")
    await _kb(client, set_id=fin["id"], name="报销")

    def call(who: str, set_ids: list[str] | None) -> Any:
        return client.post(
            "/internal/v1/knowledge-sets/readable",
            json={"set_ids": set_ids},
            headers={"X-Uid": who, "X-Auth-Uid": who},
        )

    # 全部：alice 能读三个库、两个知识集；空的知识集不返回
    data = (await call("alice", None)).json()["data"]
    assert [s["name"] for s in data] == ["人事制度", "财务"]
    assert data[0]["top_k"] == 5 and [d["name"] for d in data[0]["datasets"]] == ["考勤", "薪酬"]
    assert {"document_count", "updated_at", "description"} <= set(data[0]["datasets"][0])

    # bob 只能读公有的那个
    data = (await call("bob", [hr["id"], fin["id"], empty["id"]])).json()["data"]
    assert [(s["id"], [d["id"] for d in s["datasets"]]) for s in data] == [
        (hr["id"], [public["id"]])
    ]
