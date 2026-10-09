"""流程运行：发起、Agent 协同（产物入 Git）、准出 / 准入评审、驳回、打回、完成、待我处理、权限。"""

from __future__ import annotations

import subprocess
from typing import Any

from tf_testkit import Client, World, ok, published_template


async def _setup(w: World, *, agents: bool = True) -> tuple[str, str]:
    """团队（李工管理员；王工、赵工、周工成员）+ 模板 + 项目，角色全部分配。→ (项目 id, 团队 id)"""
    admin = await w.as_("padmin")
    tpl = await published_template(admin)
    members = [{"type": "USER", "id": w.id(a)} for a in ("dev", "qa", "lead2")]
    team = ok(
        await admin.post(
            "/api/v1/teams", {"name": "交易中台", "admins": [w.id("lead")], "members": members}
        ),
        201,
    )
    lead = await w.as_("lead")
    tid = team["id"]
    ag = None
    if agents:
        aid = w.atlas.add("全能 Agent")
        ag = ok(await lead.post(f"/api/v1/teams/{tid}/agents", {"atlas_agent_id": aid}), 201)["id"]
    p = ok(
        await lead.post(
            f"/api/v1/teams/{tid}/projects", {"name": "账号升级", "flow_template_id": tpl["id"]}
        ),
        201,
    )
    pid = p["id"]
    for role in ("产品", "架构", "测试", "开发"):
        ok(
            await lead.put(
                f"/api/v1/projects/{pid}/roles/{role}",
                {"users": [w.id("dev")], "agents": [ag] if ag else []},
            )
        )
    ok(await lead.put(f"/api/v1/projects/{pid}/roles/产品评审", {"users": [w.id("qa")]}))
    ok(
        await lead.put(
            f"/api/v1/projects/{pid}/roles/技术评审", {"users": [w.id("lead"), w.id("qa")]}
        )
    )
    return pid, tid


async def _node(c: Client, proc: str, nid: str) -> dict[str, Any]:
    return ok(await c.get(f"/api/v1/processes/{proc}/nodes/{nid}"))


async def _status(c: Client, proc: str) -> dict[str, str]:
    d = ok(await c.get(f"/api/v1/processes/{proc}"))
    return {k: v["status"] for k, v in d["nodes"].items()}


async def _pass(w: World, proc: str, nid: str, dev: Client, voters: list[Client]) -> None:
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/{nid}/submit-review"))
    for v in voters:
        ok(await v.post(f"/api/v1/processes/{proc}/nodes/{nid}/votes", {"decision": "approve"}))


async def test_process_end_to_end() -> None:
    w = World()
    pid, _ = await _setup(w)
    dev, qa, lead = await w.as_("dev"), await w.as_("qa"), await w.as_("lead")

    p = ok(
        await dev.post(
            f"/api/v1/projects/{pid}/processes",
            {"title": "支持手机号登录", "requirement": "用户可以用手机号 + 验证码登录"},
        ),
        201,
    )
    proc = p["id"]
    assert p["no"] == 1 and p["waiting"][0]["node"] == "需求分析"
    await w.settle()

    # 开工上下文发给了 Agent；Agent 的产物进了 Git
    n1 = await _node(dev, proc, "n1")
    assert n1["status"] == "working" and n1["round"] == 1
    kickoff = next(iter(w.atlas.inputs.values()))[0]
    assert "支持手机号登录" in kickoff and "需求分析" in kickoff and "<artifact>" in kickoff
    assert [m["role"] for m in n1["messages"]] == ["system", "agent"]
    assert n1["artifacts"][0]["version"] == 1 and n1["current_content"].startswith("# 产物")
    repo = w.git_root / _team_of(w, pid) / pid
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True
    ).stdout
    assert "支持手机号登录 · 需求分析 · 需求文档 v1" in log

    # 待我处理
    assert [t["kind"] for t in ok(await dev.get("/api/v1/todo"))] == ["work"]
    assert ok(await qa.get("/api/v1/todo")) == []

    # 人下指令 → Agent 修订 → 新版本
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "补充异常场景"}), 201)
    await w.settle()
    assert (await _node(dev, proc, "n1"))["artifacts"][0]["version"] == 2
    # 非执行人不能下指令
    assert (
        await qa.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "x"})
    ).status_code == 403

    # 准出：产品评审（赵工）驳回 → 回到协同，批注发给 Agent
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    assert [t["kind"] for t in ok(await qa.get("/api/v1/todo"))] == ["exit_review"]
    assert (
        await lead.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"})
    ).json()["code"] == "NOT_REVIEWER"
    r = await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "reject"})
    assert r.status_code == 422  # 驳回必须写意见
    ok(
        await qa.post(
            f"/api/v1/processes/{proc}/nodes/n1/votes",
            {"decision": "reject", "comment": "缺少风控要求"},
        )
    )
    n1 = await _node(dev, proc, "n1")
    assert n1["status"] == "working" and n1["round"] == 2
    await w.settle()
    thread = next(iter(w.atlas.inputs))
    assert "缺少风控要求" in w.atlas.inputs[thread][-1]
    assert (await _node(dev, proc, "n1"))["artifacts"][0]["round"] == 2

    # 再次准出通过 → 准入（技术评审，所有人同意：李工 + 赵工）
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    ok(await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"}))
    assert (await _status(dev, proc))["n1"] == "admit_review"
    ok(await lead.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"}))
    assert (
        await lead.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"})
    ).json()["code"] == "ALREADY_VOTED"
    assert (await _status(dev, proc))["n1"] == "admit_review"
    ok(await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"}))
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "working", "n3": "working", "n4": "pending"}  # 并行开工
    await w.settle()

    # n2、n3 通过（准出任一人、准入所有人）→ 汇合到 n4，输入是两个上游产物
    for nid in ("n2", "n3"):
        await _pass(w, proc, nid, dev, [lead, lead, qa])
    st = await _status(dev, proc)
    assert st["n4"] == "working"
    await w.settle()
    n4_kickoff = w.atlas.inputs[list(w.atlas.inputs)[-1]][0]
    assert '<input name="设计文档"' in n4_kickoff and '<input name="测试方案"' in n4_kickoff

    # n4 准出驳回并打回到 n2：n2 回到协同，n4 已打回，n3 仍已通过
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n4/submit-review"))
    r = await lead.post(
        f"/api/v1/processes/{proc}/nodes/n4/votes",
        {"decision": "reject", "comment": "x", "return_to": "n4"},
    )
    assert r.json()["code"] == "INVALID_RETURN_TARGET"
    ok(
        await lead.post(
            f"/api/v1/processes/{proc}/nodes/n4/votes",
            {"decision": "reject", "comment": "接口设计不合理", "return_to": "n2"},
        )
    )
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "working", "n3": "passed", "n4": "returned"}
    await w.settle()
    n2 = await _node(dev, proc, "n2")
    assert n2["round"] == 2 and n2["artifacts"][0]["round"] == 2

    # n2 重新通过 → n4 重新开工（第 2 轮，收到更新后的上游）→ 通过 → 流程完成
    await _pass(w, proc, "n2", dev, [lead, lead, qa])
    assert (await _status(dev, proc))["n4"] == "working"
    await w.settle()
    assert "上游产物已更新" in w.atlas.inputs[list(w.atlas.inputs)[-1]][-1]
    await _pass(w, proc, "n4", dev, [qa])
    d = ok(await dev.get(f"/api/v1/processes/{proc}"))
    assert d["status"] == "completed" and d["progress"] == {"passed": 4, "total": 4}
    assert ok(await dev.get("/api/v1/todo")) == []

    # 事件流按序完整
    lst = ok(await dev.get(f"/api/v1/projects/{pid}/processes", params={"filter": "done"}))
    assert [x["title"] for x in lst] == ["支持手机号登录"]


async def test_no_agent_failure_and_terminate() -> None:
    w = World()
    pid, _ = await _setup(w, agents=False)
    dev, qa, lead, outsider = (
        await w.as_("dev"),
        await w.as_("qa"),
        await w.as_("lead"),
        await w.as_("outsider"),
    )
    proc = ok(
        await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A", "requirement": "B"}), 201
    )["id"]
    n1 = await _node(dev, proc, "n1")
    assert "没有分配 Agent" in n1["notice"]
    assert (await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "x"})).json()[
        "code"
    ] == "NO_AGENT"
    assert (await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review")).json()[
        "code"
    ] == "NO_ARTIFACT"
    # 人直接提交产物
    ok(
        await dev.post(
            f"/api/v1/processes/{proc}/nodes/n1/artifacts",
            {"content": "# 需求\n手写", "note": "人工"},
        ),
        201,
    )
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    # 非团队成员看不到；只有发起人或团队管理员能终止
    assert (await outsider.get(f"/api/v1/processes/{proc}")).status_code == 404
    assert (await qa.post(f"/api/v1/processes/{proc}/terminate", {})).json()[
        "code"
    ] == "TERMINATE_DENIED"
    ok(await lead.post(f"/api/v1/processes/{proc}/terminate", {"reason": "需求取消"}))
    assert (
        await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"})
    ).json()["code"] == "PROCESS_NOT_RUNNING"
    assert ok(await qa.get("/api/v1/todo")) == []


async def test_agent_failure_sets_notice_and_recovers() -> None:
    w = World()
    pid, _ = await _setup(w)
    dev = await w.as_("dev")
    w.atlas.fail_next = True
    proc = ok(
        await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A", "requirement": "B"}), 201
    )["id"]
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    assert "Agent 运行失败" in n1["notice"] and n1["messages"][-1]["status"] == "failed"
    assert n1["artifacts"] == []
    # 人再发一条指令 → 恢复
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "请重试"}), 201)
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    assert n1["notice"] is None and n1["artifacts"][0]["version"] == 1


def _team_of(w: World, pid: str) -> str:
    return next(p.name for p in w.git_root.iterdir())
