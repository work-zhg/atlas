"""人机协同代理：对话组件经 …/nodes/{node}/atlas/v1/* 读写该节点的 Atlas 会话。"""

from __future__ import annotations

from atlas_teamflow.db.models import ProcessNode
from atlas_teamflow.db.session import get_sessionmaker
from sqlalchemy import update
from test_tf_process import _instruct, _node, _setup
from tf_testkit import World, ok


async def _started(w: World) -> tuple[str, str, str]:
    """发起流程、执行人第一次说明、等 Agent 交付 → (流程 id, 节点会话 id, 首个运行 id)。"""
    pid, _ = await _setup(w)
    dev = await w.as_("dev")
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    await w.settle()
    await _instruct(w, dev, proc, "n1", "写需求文档")
    n1 = await _node(dev, proc, "n1")
    thread = n1["atlas_thread_id"]
    assert thread and thread in w.atlas.threads
    run = next(r for r, t in w.atlas.run_thread.items() if t == thread)
    return proc, thread, run


async def test_read_send_and_artifact() -> None:
    w = World()
    proc, thread, run = await _started(w)
    base = f"/api/v1/processes/{proc}/nodes/n1/atlas/v1"
    dev, qa, outsider = await w.as_("dev"), await w.as_("qa"), await w.as_("outsider")
    # 监督器建会话以流程发起人的名义（内网身份传递 §10）：会话在 Atlas 里归他
    assert ("create_thread", {"X-Uid": w.id("dev"), "X-Caller-App": "teamflow"}) in w.atlas.calls

    # 读：团队成员都可以（含非执行人）
    assert ok(await qa.get(f"{base}/threads/{thread}"))["agent_name"] == "全能 Agent"
    msgs = ok(await qa.get(f"{base}/threads/{thread}/messages"))["data"]
    assert [m["role"] for m in msgs] == ["assistant", "user"]  # Atlas 倒序
    assert ok(await qa.get(f"{base}/runs/{run}"))["thread_id"] == thread
    r = await qa.get(f"{base}/threads/{thread}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "run.finished" in r.text

    # 非团队成员：看不到流程（Atlas 信封格式）
    r = await outsider.get(f"{base}/threads/{thread}")
    assert r.status_code == 404 and "error" in r.json()  # 不暴露流程是否存在

    # 写：只有执行人
    body = {"content": [{"type": "text", "text": "补充异常场景"}]}
    r = await qa.post(f"{base}/threads/{thread}/runs", body)
    assert r.status_code == 403 and r.json()["error"]["kind"] == "not_executor"

    # 执行人发指令 → 登记为节点当前运行 → 监督器跟随到结束（Agent 第二次提交；TeamFlow 不看仓库）
    w.atlas.calls.clear()
    r = await dev.post(f"{base}/threads/{thread}/runs", body, headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 202, r.text
    # 代理以当前用户的名义调 Atlas（X-Caller-App: teamflow，Atlas 信任 TeamFlow 的执行角色校验）
    assert w.atlas.calls[0] == (
        "create_run",
        {"X-Uid": w.id("dev"), "X-Caller-App": "teamflow"},
    )
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    assert n1["artifacts"] == [] and not n1["agent_running"]
    assert len(await w.repo.commits(next(iter(w.repo.repos)), path=n1["artifact_path"])) == 2
    assert w.atlas.inputs[thread][-1] == "补充异常场景"
    users = [m for m in n1["messages"] if m["role"] == "user"]
    assert users[-1]["content"] == "补充异常场景" and users[-1]["author"] == "王工"

    # 幂等键重发：同一个运行，不重复登记
    r = await dev.post(f"{base}/threads/{thread}/runs", body, headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 202
    await w.settle()
    assert sum(m["role"] == "user" for m in (await _node(dev, proc, "n1"))["messages"]) == len(
        users
    )


async def test_scope_and_state_rules() -> None:
    w = World()
    proc, thread, _ = await _started(w)
    base = f"/api/v1/processes/{proc}/nodes/n1/atlas/v1"
    dev = await w.as_("dev")

    # 只放行该节点的会话与运行
    other = await w.atlas.create_thread("agent-x", "别的会话")
    r = await dev.get(f"{base}/threads/{other}")
    assert r.status_code == 404 and r.json()["error"]["kind"] == "not_found"
    other_run = await w.atlas.create_run(other, "x", "k-other")
    assert (await dev.get(f"{base}/runs/{other_run}")).status_code == 404
    assert (await dev.get(f"{base}/agents")).status_code == 404  # 不支持的接口
    # 节点 n2 还没开工：没有会话
    r = await dev.get(f"/api/v1/processes/{proc}/nodes/n2/atlas/v1/threads/{thread}")
    assert r.json()["error"]["kind"] == "no_thread"

    # Agent 正在运行时不能再发
    async with get_sessionmaker()() as s:
        await s.execute(
            update(ProcessNode)
            .where(ProcessNode.atlas_thread_id == thread)
            .values(atlas_run_id="busy")
        )
        await s.commit()
    body = {"content": [{"type": "text", "text": "x"}]}
    r = await dev.post(f"{base}/threads/{thread}/runs", body)
    assert r.status_code == 409 and r.json()["error"]["kind"] == "run_in_progress"
    async with get_sessionmaker()() as s:
        await s.execute(
            update(ProcessNode)
            .where(ProcessNode.atlas_thread_id == thread)
            .values(atlas_run_id=None)
        )
        await s.commit()

    # 节点进入评审后不能再下指令
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    r = await dev.post(f"{base}/threads/{thread}/runs", body)
    assert r.status_code == 409 and r.json()["error"]["kind"] == "node_not_working"
