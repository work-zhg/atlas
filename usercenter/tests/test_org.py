"""组织：树规则、负责人约束与自动清空、直属上级计算、移动与删除。"""

from __future__ import annotations

from uc_testkit import Client, admin_client, make_dept, make_user, root_id


async def _manager(c: Client, uid: str) -> str | None:
    m = (await c.get(f"/api/v1/users/{uid}")).json()["manager"]
    return m["name"] if m else None


async def test_tree_rules() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    r = await c.post("/api/v1/depts", {"parent_id": root, "name": "研发中心"})
    assert r.json()["code"] == "DEPT_NAME_EXISTS"
    r = await c.post("/api/v1/depts", {"parent_id": root, "name": "a/b"})
    assert r.json()["code"] == "VALIDATION_FAILED"
    trade = await make_dept(c, rd, "交易中台")
    r = await c.post(f"/api/v1/depts/{rd}/move", {"parent_id": trade})
    assert r.json()["code"] == "MOVE_INTO_SUBTREE"
    r = await c.delete(f"/api/v1/depts/{root}")
    assert r.json()["code"] == "ROOT_IMMUTABLE"
    r = await c.delete(f"/api/v1/depts/{rd}")
    assert r.json()["code"] == "DEPT_NOT_EMPTY"
    assert (await c.delete(f"/api/v1/depts/{trade}")).status_code == 200
    await c.close()


async def test_depth_limit() -> None:
    c = await admin_client()
    parent = await root_id(c)
    for i in range(9):  # 根是第 1 级，再建 9 级到第 10 级
        parent = await make_dept(c, parent, f"L{i + 2}")
    r = await c.post("/api/v1/depts", {"parent_id": parent, "name": "L11"})
    assert r.json()["code"] == "DEPTH_EXCEEDED"
    await c.close()


async def test_leader_constraints_and_manager_chain() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    trade = await make_dept(c, rd, "交易中台")
    product = await make_dept(c, root, "产品部")
    ceo, _ = await make_user(c, "lin.yuan", root, "林远")
    li, _ = await make_user(c, "li.gong", trade, "李工")
    he, _ = await make_user(c, "he.chao", trade, "何超")
    zhang, _ = await make_user(c, "zhang.ming", product, "张明")

    # 负责人须在子树内
    r = await c.patch(f"/api/v1/depts/{trade}", {"leader_id": zhang})
    assert r.json()["code"] == "LEADER_NOT_IN_DEPT"
    assert (await c.patch(f"/api/v1/depts/{root}", {"leader_id": ceo})).status_code == 200
    assert (
        await c.patch(f"/api/v1/depts/{rd}", {"leader_id": li})
    ).status_code == 200  # 下级部门成员可负责上级
    assert (await c.patch(f"/api/v1/depts/{trade}", {"leader_id": li})).status_code == 200

    assert await _manager(c, he) == "李工"  # 本部门负责人
    assert await _manager(c, li) == "林远"  # 连续跳过本人
    assert await _manager(c, ceo) is None  # 根部门负责人

    # 换负责人：何超自己当交易中台负责人 → 上级部门负责人李工
    assert (await c.patch(f"/api/v1/depts/{trade}", {"leader_id": he})).status_code == 200
    assert await _manager(c, he) == "李工"

    # 李工调出研发中心：研发中心负责人自动清空，何超的直属上级变为林远
    r = await c.patch(f"/api/v1/users/{li}", {"dept_id": product})
    assert r.json()["leaders_cleared"] == ["研发中心"]
    assert await _manager(c, he) == "林远"

    # 停用的负责人被跳过
    assert (await c.patch(f"/api/v1/depts/{product}", {"leader_id": zhang})).status_code == 200
    assert await _manager(c, li) == "张明"
    await c.post(f"/api/v1/users/{zhang}/disable", {})
    assert await _manager(c, li) == "林远"
    assert (await c.patch(f"/api/v1/depts/{product}", {"leader_id": None})).status_code == 200
    r = await c.patch(f"/api/v1/depts/{product}", {"leader_id": zhang})
    assert r.json()["code"] == "LEADER_DISABLED"
    await c.close()


async def test_move_dept_clears_ancestor_leader_and_dry_run() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    infra = await make_dept(c, rd, "技术中心")
    chen, _ = await make_user(c, "chen.yun", infra, "陈运")
    await c.patch(f"/api/v1/depts/{rd}", {"leader_id": chen})
    r = await c.post(f"/api/v1/depts/{infra}/move", {"parent_id": root}, params={"dry_run": "true"})
    assert r.json()["leaders_to_clear"] == ["研发中心"]
    # 预演不落库
    assert (await c.get(f"/api/v1/depts/{rd}")).json()["leader"]["name"] == "陈运"
    r = await c.post(f"/api/v1/depts/{infra}/move", {"parent_id": root})
    assert r.json()["path_after"] == "星海科技 / 技术中心"
    assert (await c.get(f"/api/v1/depts/{rd}")).json()["leader"] is None
    assert (await c.get(f"/api/v1/depts/{infra}")).json()["depth"] == 2
    await c.close()


async def test_members_and_move_in() -> None:
    c = await admin_client()
    root = await root_id(c)
    rd = await make_dept(c, root, "研发中心")
    qa = await make_dept(c, rd, "质量部")
    wang, _ = await make_user(c, "wang.ce", root)
    assert (
        await c.post(f"/api/v1/depts/{qa}/members/move-in", {"user_ids": [wang]})
    ).status_code == 200
    m = (await c.get(f"/api/v1/depts/{rd}/members", params={"include_sub": "true"})).json()
    assert [u["account"] for u in m["items"]] == ["wang.ce"]
    m = (await c.get(f"/api/v1/depts/{rd}/members", params={"include_sub": "false"})).json()
    assert m["total"] == 0
    tree = {d["name"]: d for d in (await c.get("/api/v1/depts/tree")).json()}
    assert tree["研发中心"]["total_count"] == 1 and tree["研发中心"]["direct_count"] == 0
    await c.close()
