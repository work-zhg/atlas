"""流程运行：发起、Agent 协同（Agent 在 Pod 里用 git 提交产物，提交评审时锁定 commit）、
准出 / 准入评审、驳回、打回、完成、待我处理、权限。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_teamflow.db.models import ProcessNode
from atlas_teamflow.db.session import get_sessionmaker
from sqlalchemy import update
from tf_fakes import MemoryRepoHost
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


async def _instruct(w: World, dev: Client, proc: str, nid: str, text: str = "开始") -> None:
    """执行人给节点的 Agent 下一条指令（没有上游的节点第一次说明时才启动 Agent）。"""
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/{nid}/messages", {"text": text}), 201)
    await w.settle()


async def _exit(proc: str, nid: str, dev: Client, voter: Client) -> None:
    """提交准出评审并由一位评审人同意（准出规则：任一人同意）。"""
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/{nid}/submit-review"))
    ok(await voter.post(f"/api/v1/processes/{proc}/nodes/{nid}/votes", {"decision": "approve"}))


async def _admit(proc: str, nid: str, voters: list[Client]) -> None:
    """下游节点 nid 的准入评审：各评审人同意。"""
    for v in voters:
        ok(await v.post(f"/api/v1/processes/{proc}/nodes/{nid}/votes", {"decision": "approve"}))


async def test_process_end_to_end() -> None:
    w = World()
    pid, _ = await _setup(w)
    dev, qa, lead = await w.as_("dev"), await w.as_("qa"), await w.as_("lead")

    p = ok(
        await dev.post(
            f"/api/v1/projects/{pid}/processes",
            {"title": "支持手机号登录"},
        ),
        201,
    )
    proc = p["id"]
    assert p["no"] == 1 and p["waiting"][0]["node"] == "需求分析"
    await w.settle()

    # 没有上游的节点：开工但不空跑 Agent（不建会话、不投递），等执行人第一次说明
    n1 = await _node(dev, proc, "n1")
    assert n1["status"] == "working" and n1["round"] == 1
    assert n1["artifact_path"] == "0001-支持手机号登录/01-需求分析/需求文档.md"
    assert n1["atlas_thread_id"] is None and n1["messages"] == [] and not w.atlas.inputs

    # 执行人第一次说明 → 开工上下文 + 说明合成一条投递（仓库地址、产物路径、git 操作说明）
    await _instruct(w, dev, proc, "n1", "用户可以用手机号 + 验证码登录")
    n1 = await _node(dev, proc, "n1")
    first = w.atlas.inputs[n1["atlas_thread_id"]]
    assert len(first) == 1  # 没有空跑的一轮
    kickoff = first[0]
    assert "支持手机号登录" in kickoff and "需求分析" in kickoff and "需求：" not in kickoff
    assert kickoff.endswith("执行人的说明：\n用户可以用手机号 + 验证码登录")
    assert "<repository" in kickoff and "git clone" in kickoff and "TeamFlow-Node: n1" in kickoff
    assert [(m["role"], m["author"]) for m in n1["messages"]] == [("user", "王工"), ("agent", None)]
    assert n1["artifacts"] == [] and n1["current_content"] is None  # 提交评审时才锁定版本
    commit = w.repo.last(_repo(w, pid))
    assert commit.message.startswith("支持手机号登录 · 需求分析 · 第 1 轮")
    # 只给 Atlas 会话指定 git 凭据档案，TeamFlow 自己不碰凭据
    assert w.atlas.thread_profiles and set(w.atlas.thread_profiles.values()) == {"teamflow-gitee"}

    # 待我处理
    assert [t["kind"] for t in ok(await dev.get("/api/v1/todo"))] == ["work"]
    assert ok(await qa.get("/api/v1/todo")) == []

    # 再下指令 → 不再拼开工上下文；Agent 修订并再次提交（仓库里两个提交，仍未锁定版本）
    await _instruct(w, dev, proc, "n1", "补充异常场景")
    assert w.atlas.inputs[n1["atlas_thread_id"]][-1] == "补充异常场景"
    path = n1["artifact_path"]
    assert len(await w.repo.commits(_repo(w, pid), path=path)) == 2
    assert (await _node(dev, proc, "n1"))["artifacts"] == []
    # 非执行人不能下指令
    assert (
        await qa.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "x"})
    ).status_code == 403

    # 准出：锁定产物路径最新的 commit 为 v1
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    a1 = (await _node(dev, proc, "n1"))["artifacts"][0]
    assert a1["version"] == 1 and a1["source"] == "agent" and a1["round"] == 1
    assert a1["commit"] == w.repo.last(_repo(w, pid)).sha and a1["url"].endswith(a1["commit"])
    # 产品评审（赵工）驳回 → 回到协同，批注发给 Agent
    assert [t["kind"] for t in ok(await qa.get("/api/v1/todo"))] == ["exit_review"]
    assert (
        await lead.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"})
    ).json()["code"] == "NOT_REVIEWER"
    r = await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "reject"})
    assert r.status_code == 422  # 驳回必须写意见
    w.atlas.commits = False  # 驳回后的这一轮 Agent 没有提交
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
    r = await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review")
    assert r.json()["code"] == "ARTIFACT_NOT_REVISED"
    w.atlas.commits = True
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "请提交"}), 201)
    await w.settle()

    # 再次准出（锁定 v2）通过 → n1 待下游准入；下游 n2、n3 各自发起准入（技术评审，所有人同意）
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    assert (await _node(dev, proc, "n1"))["artifacts"][0]["round"] == 2
    ok(await qa.post(f"/api/v1/processes/{proc}/nodes/n1/votes", {"decision": "approve"}))
    st = await _status(dev, proc)
    assert st == {"n1": "exited", "n2": "admit_review", "n3": "admit_review", "n4": "pending"}
    assert [t["kind"] for t in ok(await qa.get("/api/v1/todo"))] == ["admit_review"] * 2
    # n2 准入：李工、赵工都同意 → n2 开工；n3 还没准入，n1 仍待下游准入
    ok(await lead.post(f"/api/v1/processes/{proc}/nodes/n2/votes", {"decision": "approve"}))
    assert (
        await lead.post(f"/api/v1/processes/{proc}/nodes/n2/votes", {"decision": "approve"})
    ).json()["code"] == "ALREADY_VOTED"
    assert (await _status(dev, proc))["n2"] == "admit_review"
    ok(await qa.post(f"/api/v1/processes/{proc}/nodes/n2/votes", {"decision": "approve"}))
    st = await _status(dev, proc)
    assert st == {"n1": "exited", "n2": "working", "n3": "admit_review", "n4": "pending"}
    # n3 准入通过 → n1 的下游都开工了，n1 完成
    await _admit(proc, "n3", [lead, qa])
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "working", "n3": "working", "n4": "pending"}
    await w.settle()

    # n2 先准出 → 待下游准入（n3 还没准出，n4 不发起准入）
    await _exit(proc, "n2", dev, lead)
    assert (await _status(dev, proc))["n4"] == "pending"
    # n3 准出 → 汇合节点 n4 发起准入，一次审核两个上游产物
    await _exit(proc, "n3", dev, lead)
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "exited", "n3": "exited", "n4": "admit_review"}
    n4 = await _node(dev, proc, "n4")
    assert {i["output"] for i in n4["inputs"]} == {"设计文档", "测试方案"}

    # n4 准入驳回：两个上游，必须指定打回哪个；打回 n2 → n2 回到协同，n4 已打回，n3 仍待下游准入
    r = await lead.post(
        f"/api/v1/processes/{proc}/nodes/n4/votes", {"decision": "reject", "comment": "x"}
    )
    assert r.json()["code"] == "RETURN_TARGET_REQUIRED"
    ok(
        await lead.post(
            f"/api/v1/processes/{proc}/nodes/n4/votes",
            {"decision": "reject", "comment": "接口设计不合理", "return_to": "n2"},
        )
    )
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "working", "n3": "exited", "n4": "returned"}
    await w.settle()
    n2_thread = (await _node(dev, proc, "n2"))["atlas_thread_id"]
    assert "准入评审时打回到本节点" in w.atlas.inputs[n2_thread][-1]

    # n2 重新准出 → n4 重新发起准入 → 通过 → n4 开工，n2、n3 完成
    await _exit(proc, "n2", dev, lead)
    assert (await _node(dev, proc, "n2"))["artifacts"][0]["round"] == 2
    await _admit(proc, "n4", [lead, qa])
    st = await _status(dev, proc)
    assert st == {"n1": "passed", "n2": "passed", "n3": "passed", "n4": "working"}
    await w.settle()
    n4_kickoff = w.atlas.inputs[list(w.atlas.inputs)[-1]][0]
    assert '<input name="设计文档"' in n4_kickoff and '<input name="测试方案"' in n4_kickoff

    # 最后一个节点：准出通过即完成 → 流程完成
    await _exit(proc, "n4", dev, qa)
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
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    n1 = await _node(dev, proc, "n1")
    assert "没有分配 Agent" in n1["notice"]
    assert (await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "x"})).json()[
        "code"
    ] == "NO_AGENT"
    assert (await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review")).json()[
        "code"
    ] == "NO_ARTIFACT"
    # 人在本地提交产物（没绑定 Gitee 账号 → 来源未知），提交评审时锁定
    path = (await _node(dev, proc, "n1"))["artifact_path"]
    w.repo.push(_repo(w, pid), {path: "# 需求\n手写"}, "手写需求", author_login="someone")
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    a = (await _node(dev, proc, "n1"))["artifacts"][0]
    assert a["source"] == "unknown" and a["author_login"] == "someone"
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
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    await w.settle()
    w.atlas.fail_next = True
    await _instruct(w, dev, proc, "n1", "写一份需求文档")
    n1 = await _node(dev, proc, "n1")
    assert "Agent 运行失败" in n1["notice"] and n1["messages"][-1]["status"] == "failed"
    assert n1["artifacts"] == []
    # 人再发一条指令 → 恢复
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "请重试"}), 201)
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    assert n1["notice"] is None
    assert await w.repo.commits(_repo(w, pid), path=n1["artifact_path"])


async def test_agent_without_shell_gets_notice() -> None:
    """产物要由 Agent 用 git 提交：不能执行命令的 Agent 在开工时就提示。"""
    w = World()
    aid = w.atlas.add("纯对话 Agent", kind="native")
    pid, tid = await _setup(w, agents=False)
    lead, dev = await w.as_("lead"), await w.as_("dev")
    ag = ok(await lead.post(f"/api/v1/teams/{tid}/agents", {"atlas_agent_id": aid}), 201)["id"]
    ok(
        await lead.put(
            f"/api/v1/projects/{pid}/roles/产品", {"users": [w.id("dev")], "agents": [ag]}
        )
    )
    w.atlas.commits = False
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    assert "不能执行命令" in n1["notice"] and n1["artifacts"] == []


def _repo(w: World, pid: str) -> str:
    return MemoryRepoHost().repo_name(UUID(pid))


async def test_admission_single_upstream_and_resume() -> None:
    """单上游的准入驳回默认打回该上游；升级后停在「待下游准入」的流程由监督器启动时推进。"""
    w = World()
    pid, _ = await _setup(w)
    dev, qa, lead = await w.as_("dev"), await w.as_("qa"), await w.as_("lead")
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    await w.settle()
    # 执行人没有和 Agent 说过话，直接在本地提交了产物并提交评审
    path = (await _node(dev, proc, "n1"))["artifact_path"]
    w.repo.push(_repo(w, pid), {path: "# 需求\n手写"}, "手写需求", author_login="someone")
    await _exit(proc, "n1", dev, qa)
    ok(
        await lead.post(
            f"/api/v1/processes/{proc}/nodes/n2/votes",
            {"decision": "reject", "comment": "需求缺少验收标准"},  # 不选打回目标
        )
    )
    st = await _status(dev, proc)
    assert st == {"n1": "working", "n2": "returned", "n3": "returned", "n4": "pending"}
    assert (await _node(dev, proc, "n1"))["round"] == 2
    # Agent 第一次收到的就是打回指令：前面补上开工上下文，不缺仓库与产物路径
    await w.settle()
    first = w.atlas.inputs[(await _node(dev, proc, "n1"))["atlas_thread_id"]]
    assert len(first) == 1 and "git clone" in first[0]
    assert "本轮指令：\n下游节点「架构设计」准入评审时打回到本节点" in first[0]

    # 模拟升级前的数据：n1 已准出（exited），下游还没有发起准入
    await w.settle()
    async with get_sessionmaker()() as s:
        await s.execute(
            update(ProcessNode)
            .where(ProcessNode.process_uuid == UUID(proc), ProcessNode.node_id == "n1")
            .values(status="exited")
        )
        await s.commit()
    await w.app.state.supervisor._advance_running()
    st = await _status(dev, proc)
    assert st["n2"] == "admit_review" and st["n3"] == "admit_review"
