"""登录、文件模板、流程模板（草稿 / 校验 / 发布 / 版本）。"""

from __future__ import annotations

from tf_testkit import World, ok, published_template, simple_definition


async def test_login_me_and_access() -> None:
    w = World()
    c = await w.as_("padmin")
    me = ok(await c.get("/api/v1/auth/me"))
    assert me["user"]["account"] == "padmin"
    assert "flow_template:manage" in me["permissions"]

    w.uc.users[w.ids["outsider"]]["access"] = False
    from tf_testkit import Client

    other = Client(w.app)
    r = await other.post("/api/v1/auth/login", {"account": "outsider", "password": "pw"})
    assert r.status_code == 403 and r.json()["code"] == "NO_ACCESS"
    r = await other.post("/api/v1/auth/login", {"account": "dev", "password": "bad"})
    assert r.status_code == 401

    # 写请求缺 CSRF 头 → 403
    r = await c.http.post("/api/v1/file-templates", json={"name": "x"})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"


async def test_file_templates() -> None:
    w = World()
    admin = await w.as_("padmin")
    dev = await w.as_("dev")
    t = ok(
        await admin.post("/api/v1/file-templates", {"name": "需求文档模板", "usage": "artifact"}),
        201,
    )
    tid = t["id"]
    assert (await dev.post("/api/v1/file-templates", {"name": "x"})).status_code == 403

    def up(c, content: bytes, name: str = "req.md", note: str = "首版"):  # type: ignore[no-untyped-def]
        return c.post(
            f"/api/v1/file-templates/{tid}/versions",
            files={"file": (name, content)},
            data={"change_note": note},
        )

    t = ok(await up(admin, "﻿# 需求\n\n## 背景\n".encode()), 201)
    assert t["current_version"]["version_no"] == 1 and t["content"].startswith("# 需求")
    r = await up(admin, "# 需求\n\n## 背景\n".encode())
    assert r.json()["code"] == "CONTENT_UNCHANGED"
    assert (await up(admin, b"%PDF", name="a.pdf")).json()["code"] == "UNSUPPORTED_FILE_TYPE"
    assert (await up(admin, b"#" * (100 * 1024 + 1))).json()["code"] == "FILE_TOO_LARGE"
    t = ok(await up(admin, "# 需求 v2\n".encode(), note="补充"), 201)
    assert [v["version_no"] for v in t["versions"]] == [2, 1]

    # 成员可查看、下载
    assert len(ok(await dev.get("/api/v1/file-templates"))) == 1
    r = await dev.get(f"/api/v1/file-templates/{tid}/versions/1/download")
    assert r.status_code == 200 and r.text.startswith("# 需求")

    # 停用后不能上传；未被引用可删除
    ok(await admin.post(f"/api/v1/file-templates/{tid}/disable"))
    assert (await up(admin, b"# v3")).json()["code"] == "TEMPLATE_DISABLED"
    ok(await admin.delete(f"/api/v1/file-templates/{tid}"))


async def test_flow_template_draft_and_publish() -> None:
    w = World()
    admin = await w.as_("padmin")
    t = ok(await admin.post("/api/v1/flow-templates", {"name": "标准研发流程"}), 201)
    tid, d = t["id"], t["draft"]
    assert t["current_version"] is None and d["editing_by_me"]

    # 空白节点：发布校验不通过
    r = await admin.post(f"/api/v1/flow-templates/{tid}/publish", {"change_note": "首版"})
    assert r.status_code == 422 and r.json()["details"]["errors"]

    defn = simple_definition()
    defn["nodes"]["n2"]["admit_review"] = None  # 有下游却无准入 → 警告
    d = ok(
        await admin.put(
            f"/api/v1/flow-templates/{tid}/draft", {"definition": defn, "version": d["version"]}
        )
    )
    assert not d["errors"] and d["warnings"]
    assert d["deps"]["n4"] == ["n2", "n3"] and d["deps"]["n2"] == ["n1"]

    # 旧版本号保存 → 冲突
    r = await admin.put(
        f"/api/v1/flow-templates/{tid}/draft", {"definition": defn, "version": d["version"] - 1}
    )
    assert r.json()["code"] == "STALE_ROW_VERSION"

    r = await admin.post(f"/api/v1/flow-templates/{tid}/publish", {"change_note": "首版"})
    assert r.json()["code"] == "WARNINGS_NOT_ACKNOWLEDGED"
    t = ok(
        await admin.post(
            f"/api/v1/flow-templates/{tid}/publish",
            {"change_note": "首版", "acknowledged_warnings": True},
        )
    )
    assert t["current_version"]["label"] == "v1.0" and not t["has_draft"]
    assert t["current_version"]["roles"] == {
        "exec": ["产品", "架构", "测试", "开发"],
        "review": ["产品评审", "技术评审"],
    }
    roles = {r["name"] for r in ok(await admin.get("/api/v1/flow-templates/roles"))}
    assert {"产品", "技术评审"} <= roles

    # 再次编辑：基于 v1.0 的草稿 → v1.1
    d = ok(await admin.post(f"/api/v1/flow-templates/{tid}/draft"))
    defn["nodes"]["n2"]["admit_review"] = {"role": "技术评审", "rule": "all"}
    d = ok(
        await admin.put(
            f"/api/v1/flow-templates/{tid}/draft", {"definition": defn, "version": d["version"]}
        )
    )
    t = ok(await admin.post(f"/api/v1/flow-templates/{tid}/publish", {"change_note": "补准入"}))
    assert [v["label"] for v in t["versions"]] == ["v1.1", "v1.0"]
    v = ok(await admin.get(f"/api/v1/flow-templates/{tid}/versions/v1.0"))
    assert v["definition"]["nodes"]["n2"]["admit_review"] is None  # 已发布版本不可变


async def test_flow_template_lock_takeover_and_rules() -> None:
    w = World()
    w.uc.users[w.ids["lead"]]["roles"].append("TF_PLATFORM_ADMIN")
    a = await w.as_("padmin")
    b = await w.as_("lead")
    t = await published_template(a)
    tid = t["id"]
    d = ok(await a.post(f"/api/v1/flow-templates/{tid}/draft"))
    # 他人正在编辑 → 不能保存，需接管
    r = await b.put(
        f"/api/v1/flow-templates/{tid}/draft",
        {"definition": d["definition"], "version": d["version"]},
    )
    assert r.json()["code"] == "DRAFT_LOCKED"
    d = ok(await b.post(f"/api/v1/flow-templates/{tid}/draft/takeover"))
    assert d["editing_by_me"]

    # 同一角色既执行又评审 → 错误
    defn = d["definition"]
    defn["nodes"]["n1"]["exit_review"]["role"] = "开发"
    d = ok(
        await b.put(
            f"/api/v1/flow-templates/{tid}/draft", {"definition": defn, "version": d["version"]}
        )
    )
    assert any("不能同时" in e["message"] for e in d["errors"])
    ok(await b.delete(f"/api/v1/flow-templates/{tid}/draft"))

    # 团队管理员可查看，不能维护
    lead2 = await w.as_("lead2")
    assert ok(await lead2.get(f"/api/v1/flow-templates/{tid}"))["name"] == "标准研发流程"
    assert (await lead2.post(f"/api/v1/flow-templates/{tid}/draft")).status_code == 403
    dev = await w.as_("dev")
    assert (await dev.get("/api/v1/flow-templates")).status_code == 403


async def test_file_template_reference_blocks_delete() -> None:
    w = World()
    admin = await w.as_("padmin")
    f = ok(await admin.post("/api/v1/file-templates", {"name": "设计文档模板"}), 201)
    t = ok(await admin.post("/api/v1/flow-templates", {"name": "流程A"}), 201)
    defn = simple_definition()
    defn["nodes"]["n2"]["artifact_file_template_id"] = f["id"]
    d = ok(
        await admin.put(
            f"/api/v1/flow-templates/{t['id']}/draft",
            {"definition": defn, "version": t["draft"]["version"]},
        )
    )
    assert any("还没有生效版本" in x["message"] for x in d["warnings"])
    ok(
        await admin.post(
            f"/api/v1/flow-templates/{t['id']}/publish",
            {"change_note": "v1", "acknowledged_warnings": True},
        )
    )
    assert ok(await admin.get(f"/api/v1/file-templates/{f['id']}"))["references"]
    assert (await admin.delete(f"/api/v1/file-templates/{f['id']}")).json()[
        "code"
    ] == "TEMPLATE_IN_USE"


async def test_open_draft_concurrently() -> None:
    """两人同时点「编辑」（或前端重复请求）：只产生一份草稿，都不报错。"""
    import asyncio

    w = World()
    admin = await w.as_("padmin")
    t = await published_template(admin)
    a, b = await asyncio.gather(
        admin.post(f"/api/v1/flow-templates/{t['id']}/draft"),
        admin.post(f"/api/v1/flow-templates/{t['id']}/draft"),
    )
    assert a.status_code == b.status_code == 200
    assert a.json()["version"] == b.json()["version"]
