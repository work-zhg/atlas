"""运行这一侧：发起、首节点开始、提交评审、准出与下游准入、驳回与打回、终止；节点协作的投递。"""

from __future__ import annotations

from typing import Any

from kit import World, ok, setup_project


async def _start(w: World, env: dict[str, Any], title: str = "支付回调幂等") -> dict[str, Any]:
    return ok(
        await w.as_("zhou").post(
            f"/api/v1/projects/{env['project']['id']}/processes",
            {"template_id": env["template"]["id"], "title": title},
        ),
        201,
    )


def _node(detail: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in detail["nodes"] if n["id"] == node_id)


def _last_run(w: World, thread_id: str) -> str:
    return w.atlas.threads[thread_id]["runs"][-1]


async def _upload(w: World, who: str, pid: str, path: str, text: str = "内容") -> None:
    ok(
        await w.as_(who).put(f"/api/v1/processes/{pid}/space?path={path}", content=text.encode()),
        201,
    )


async def test_start_opens_space_and_waits_for_begin() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    assert proc["code"] == "F-1001"
    assert ("open_workspace", proc["space_id"]) in w.atlas.calls
    detail = ok(await w.as_("zhou").get(f"/api/v1/processes/{proc['id']}"))
    assert {n["id"]: n["status"] for n in detail["nodes"]} == {
        "clarify": "pending",
        "prd": "pending",
        "code": "pending",
        "cases": "pending",
    }
    # 首节点不自动开工：还没建会话
    assert w.atlas.threads == {}
    # 不是执行人不能开始；不是首节点不能手动开始
    assert (
        await w.as_("chen").post(f"/api/v1/processes/{proc['id']}/nodes/clarify/begin")
    ).status_code == 403
    assert (
        await w.as_("zhou").post(f"/api/v1/processes/{proc['id']}/nodes/prd/begin")
    ).status_code == 409
    n = ok(await w.as_("zhou").post(f"/api/v1/processes/{proc['id']}/nodes/clarify/begin"))
    assert (n["status"], n["round"]) == ("working", 1)
    await w.settle()
    detail = ok(await w.as_("zhou").get(f"/api/v1/processes/{proc['id']}"))
    session = _node(detail, "clarify")["session"]
    thread = w.atlas.threads[session["thread_id"]]
    assert thread["space"] == proc["space_id"]
    assert thread["owner"] == str(w.ids["zhou"])  # 会话归发起人
    kickoff = w.atlas.texts(session["thread_id"])[0]
    assert "做好需求澄清" in kickoff and "「需求澄清/」" in kickoff
    # 发起时给流程里的人授了数字员工的使用权
    pm_agent = env["agents"]["pm"]["atlas_agent_id"]
    assert str(w.ids["chen"]) in w.atlas.users_with_use(pm_agent)


async def test_review_admission_and_return_upstream() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    pid = proc["id"]
    zhou, li, chen = w.as_("zhou"), w.as_("li"), w.as_("chen")
    ok(await zhou.post(f"/api/v1/processes/{pid}/nodes/clarify/begin"))
    await w.settle()

    # 提交：文件要在流程空间里
    r = await zhou.post(
        f"/api/v1/processes/{pid}/nodes/clarify/submissions", {"paths": ["需求澄清/记录.md"]}
    )
    assert r.status_code == 422 and r.json()["code"] == "FILES_MISSING"
    await _upload(w, "zhou", pid, "需求澄清/记录.md")
    rr = ok(
        await zhou.post(
            f"/api/v1/processes/{pid}/nodes/clarify/submissions", {"paths": ["需求澄清/记录.md"]}
        ),
        201,
    )
    assert rr["reviewers"] == [str(w.ids["zhou"])]
    await w.settle()
    detail = ok(await zhou.get(f"/api/v1/processes/{pid}"))
    clarify = _node(detail, "clarify")
    assert clarify["status"] == "exit_review"
    assert clarify["session"]["state"] == "frozen"
    # 冻结：最后开的 run 被取消
    assert ("cancel_run", clarify["session"]["last_run_id"]) in w.atlas.calls

    # 准出通过 → PRD 没配准入，直接开工；需求澄清的下游都开工了 → 已通过
    ok(await zhou.post(f"/api/v1/processes/{pid}/nodes/clarify/votes", {"decision": "approve"}))
    await w.settle()
    detail = ok(await zhou.get(f"/api/v1/processes/{pid}"))
    assert _node(detail, "clarify")["status"] == "passed"
    prd = _node(detail, "prd")
    assert prd["status"] == "working"
    assert "需求澄清/记录.md" in w.atlas.texts(prd["session"]["thread_id"])[0]

    # PRD 准出 → 编码、测试用例都开准入评审
    await _upload(w, "zhou", pid, "PRD/PRD.md")
    ok(
        await zhou.post(
            f"/api/v1/processes/{pid}/nodes/prd/submissions", {"paths": ["PRD/PRD.md"]}
        ),
        201,
    )
    ok(await zhou.post(f"/api/v1/processes/{pid}/nodes/prd/votes", {"decision": "approve"}))
    detail = ok(await zhou.get(f"/api/v1/processes/{pid}"))
    assert _node(detail, "prd")["status"] == "exited"
    assert _node(detail, "code")["open_review"]["stage"] == "admit"
    assert sorted(_node(detail, "code")["open_review"]["reviewers"]) == sorted(
        [str(w.ids["li"]), str(w.ids["zhou"])]
    )
    todos = ok(await chen.get("/api/v1/todos"))
    assert [(t["node_id"], t["kind"]) for t in todos] == [("cases", "admit")]

    # 编码准入通过 → 编码开工（带上游提交的文件）；测试用例还没开工，PRD 还是待下游准入
    ok(await li.post(f"/api/v1/processes/{pid}/nodes/code/votes", {"decision": "approve"}))
    await w.settle()
    detail = ok(await zhou.get(f"/api/v1/processes/{pid}"))
    code = _node(detail, "code")
    assert code["status"] == "working"
    assert "PRD/PRD.md" in w.atlas.texts(code["session"]["thread_id"])[0]
    assert _node(detail, "prd")["status"] == "exited"

    # 测试用例的准入评审人打回 PRD：意见必填
    r = await chen.post(f"/api/v1/processes/{pid}/nodes/cases/votes", {"decision": "reject"})
    assert r.status_code == 422
    ok(
        await chen.post(
            f"/api/v1/processes/{pid}/nodes/cases/votes",
            {"decision": "reject", "comment": "导出分页上限没区分租户"},
        )
    )
    await w.settle()
    detail = ok(await zhou.get(f"/api/v1/processes/{pid}"))
    prd = _node(detail, "prd")
    assert (prd["status"], prd["round"]) == ("working", 2)
    assert _node(detail, "code")["status"] == "returned"
    assert _node(detail, "cases")["status"] == "returned"
    # 编码的数字员工停下；PRD 第 2 轮在原来的会话里收到意见
    assert ("cancel_run", code["session"]["last_run_id"]) in w.atlas.calls
    texts = w.atlas.texts(prd["session"]["thread_id"])
    assert len(texts) == 2 and "导出分页上限没区分租户" in texts[1]
    events = [e["type"] for e in ok(await zhou.get(f"/api/v1/processes/{pid}/events"))]
    assert "node.returned" in events


async def test_exit_reject_is_next_round_in_same_session() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    pid = proc["id"]
    zhou = w.as_("zhou")
    ok(await zhou.post(f"/api/v1/processes/{pid}/nodes/clarify/begin"))
    await w.settle()
    await _upload(w, "zhou", pid, "需求澄清/记录.md")
    ok(
        await zhou.post(
            f"/api/v1/processes/{pid}/nodes/clarify/submissions", {"paths": ["需求澄清/记录.md"]}
        ),
        201,
    )
    ok(
        await zhou.post(
            f"/api/v1/processes/{pid}/nodes/clarify/votes",
            {"decision": "reject", "comment": "补上验收标准"},
        )
    )
    await w.settle()
    node = ok(await zhou.get(f"/api/v1/processes/{pid}/nodes/clarify"))
    assert (node["status"], node["round"]) == ("working", 2)
    assert node["session"]["state"] == "active"
    assert [i["kind"] for i in node["instructions"]] == ["kickoff", "revise"]
    assert all(i["status"] == "sent" for i in node["instructions"])
    assert "补上验收标准" in w.atlas.texts(node["session"]["thread_id"])[1]


async def test_human_message_takeover_and_handback() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    pid = proc["id"]
    zhou = w.as_("zhou")
    url = f"/api/v1/processes/{pid}/nodes/clarify"
    # 还没开始：没有会话
    msg = {"content": [{"type": "text", "text": "先按渠道流水号做"}]}
    assert (await zhou.post(f"{url}/messages", msg)).status_code == 409
    ok(await zhou.post(f"{url}/begin"))
    await w.settle()
    node = ok(await zhou.get(url))
    thread = node["session"]["thread_id"]
    assert node["session"]["can_send"] is True
    # 数字员工还在跑：Agent 平台的会话串行
    r = await zhou.post(f"{url}/messages", msg)
    assert r.status_code == 409 and r.json()["code"] == "AGENT_BUSY"
    w.atlas.finish(_last_run(w, thread))
    run = ok(await zhou.post(f"{url}/messages", {**msg, "idempotency_key": "k-1"}), 202)["run_id"]
    assert w.atlas.runs[run]["by"] == str(w.ids["zhou"])  # 以作者的身份开 run
    # 同一个幂等键重发拿到同一个 run
    w.atlas.finish(run)
    assert (
        ok(await zhou.post(f"{url}/messages", {**msg, "idempotency_key": "k-1"}), 202)["run_id"]
        == run
    )
    # 不是执行人不能发
    assert (await w.as_("chen").post(f"{url}/messages", msg)).status_code == 403

    # 接管：挡住发消息；交还：说明排成下一条指令
    ok(await zhou.post(f"{url}/takeover"))
    assert (
        await zhou.post(f"{url}/messages", {**msg, "idempotency_key": "k-2"})
    ).status_code == 409
    ok(await zhou.post(f"{url}/handback", {"note": "我改了接口签名"}))
    await w.settle()
    node = ok(await zhou.get(url))
    assert node["instructions"][-1]["kind"] == "handback"
    assert node["instructions"][-1]["status"] == "sent"
    assert "我改了接口签名" in w.atlas.texts(thread)[-1]


async def test_dispatcher_retries_when_agent_busy() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    pid = proc["id"]
    zhou = w.as_("zhou")
    url = f"/api/v1/processes/{pid}/nodes/clarify"
    ok(await zhou.post(f"{url}/begin"))
    await w.settle()
    thread = ok(await zhou.get(url))["session"]["thread_id"]
    # 接管后立刻交还：上一次的 run 取消了但还在收尾，交还指令先排着
    w.atlas.slow_cancel = True
    ok(await zhou.post(f"{url}/takeover"))
    await w.settle()
    ok(await zhou.post(f"{url}/handback", {"note": "好了"}))
    await w.settle()
    node = ok(await zhou.get(url))
    assert node["instructions"][-1]["status"] == "queued"
    assert node["instructions"][-1]["last_error"]
    # 收尾结束后扫描再投
    w.atlas.finish(_last_run(w, thread))
    await w.app.state.dispatcher.sweep()
    node = ok(await zhou.get(url))
    assert node["instructions"][-1]["status"] == "sent"


async def test_terminate_stops_everything() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    pid = proc["id"]
    ok(await w.as_("zhou").post(f"/api/v1/processes/{pid}/nodes/clarify/begin"))
    await w.settle()
    # 陈运不是发起人也不是团队管理员
    assert (await w.as_("chen").post(f"/api/v1/processes/{pid}/terminate", {})).status_code == 403
    ok(await w.as_("li").post(f"/api/v1/processes/{pid}/terminate", {"reason": "需求取消"}))
    await w.settle()
    detail = ok(await w.as_("zhou").get(f"/api/v1/processes/{pid}"))
    assert detail["status"] == "terminated"
    assert _node(detail, "clarify")["session"]["state"] == "stopped"
    # 流程空间转只读
    r = await w.as_("zhou").put(f"/api/v1/processes/{pid}/space?path=x.md", content=b"x")
    assert r.status_code == 409


async def test_read_only_member_can_view_but_not_run() -> None:
    w = World()
    env = await setup_project(w)
    proc = await _start(w, env)
    he = w.as_("he")
    ok(await he.get(f"/api/v1/processes/{proc['id']}"))
    r = await he.post(
        f"/api/v1/projects/{env['project']['id']}/processes",
        {"template_id": env["template"]["id"], "title": "x"},
    )
    assert r.status_code == 403
