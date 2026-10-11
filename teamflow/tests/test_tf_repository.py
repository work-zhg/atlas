"""产物仓库：建仓库、成员绑定 Gitee 账号、协作者同步、提交评审锁定 commit、流程目录预览。"""

from __future__ import annotations

from typing import Any

from test_tf_process import _node, _repo, _setup
from tf_fakes import FakeOAuth
from tf_testkit import Client, World, ok


async def _bind(w: World, c: Client, code: str) -> dict[str, Any]:
    start = ok(await c.post("/api/v1/me/git-account/oauth"))
    state = start["url"].split("state=")[1]
    return ok(await c.post("/api/v1/me/git-account/oauth/callback", {"code": code, "state": state}))


def _oauth() -> FakeOAuth:
    o = FakeOAuth()
    o.users = {
        "c-dev": ("wang-gitee", "101"),
        "c-lead": ("li-gitee", "102"),
        "c-out": ("out", "103"),
    }
    return o


async def test_bind_account_and_sync_collaborators() -> None:
    w = World(oauth=_oauth())
    pid, tid = await _setup(w)
    repo = _repo(w, pid)
    assert w.repo.collab[repo] == {}  # 建项目时已建仓库；还没人绑定账号
    dev, lead, outsider = await w.as_("dev"), await w.as_("lead"), await w.as_("outsider")

    # state 不对 → 拒绝；绑定后按团队身份加为协作者（成员、管理员默认都只读）
    ok(await dev.post("/api/v1/me/git-account/oauth"))
    r = await dev.post("/api/v1/me/git-account/oauth/callback", {"code": "c-dev", "state": "x"})
    assert r.json()["code"] == "GITEE_OAUTH_STATE"
    me = await _bind(w, dev, "c-dev")
    assert me["account"]["login"] == "wang-gitee"
    assert me["repositories"] == [
        {"login": "wang-gitee", "permission": "pull", "error": None, "project_id": pid}
    ]
    await _bind(w, lead, "c-lead")
    assert w.repo.collab[repo] == {"wang-gitee": "pull", "li-gitee": "pull"}
    # 同一个 Gitee 账号不能被两个人绑定；非团队成员绑定后不会成为协作者
    r = await outsider.post("/api/v1/me/git-account/oauth")
    state = r.json()["url"].split("state=")[1]
    r = await outsider.post(
        "/api/v1/me/git-account/oauth/callback", {"code": "c-dev", "state": state}
    )
    assert r.json()["code"] == "GIT_ACCOUNT_TAKEN"
    await _bind(w, outsider, "c-out")
    assert "out" not in w.repo.collab[repo]

    # 项目开启「允许管理员直接提交」→ 管理员变为 push；仓库视图里看得到
    d = ok(await lead.get(f"/api/v1/projects/{pid}"))
    ok(
        await lead.patch(
            f"/api/v1/projects/{pid}", {"allow_admin_commit": True, "version": d["version"]}
        )
    )
    assert w.repo.collab[repo]["li-gitee"] == "push"
    view = ok(await lead.get(f"/api/v1/projects/{pid}/repository"))
    assert view["status"] == "ready" and view["clone_url"].endswith(f"{repo}.git")
    assert view["me"] == {"login": "li-gitee", "permission": "push", "error": None}
    assert {c["login"] for c in view["collaborators"]} == {"wang-gitee", "li-gitee"}
    mine = ok(await dev.get(f"/api/v1/projects/{pid}/repository"))
    assert mine["collaborators"] is None and not mine["can_sync"]  # 成员看不到协作者列表

    # 移出团队 → 从仓库移除；解绑 → 从仓库移除
    grants = ok(await lead.get(f"/api/v1/teams/{tid}/members"))["grants"]
    row = next(g for g in grants if g["subject"]["id"] == w.id("dev"))
    ok(await lead.delete(f"/api/v1/teams/{tid}/members/{row['id']}"))
    assert "wang-gitee" not in w.repo.collab[repo]
    ok(await lead.delete("/api/v1/me/git-account"))
    assert w.repo.collab[repo] == {}

    # 有人在 Gitee 上手工删掉了协作者 → 管理员手动同步补齐
    await _bind(w, lead, "c-lead")
    w.repo.collab[repo].clear()
    ok(await lead.post(f"/api/v1/projects/{pid}/repository/sync"))
    assert w.repo.collab[repo] == {"li-gitee": "push"}
    qa = await w.as_("qa")  # 普通成员不能同步
    assert (await qa.post(f"/api/v1/projects/{pid}/repository/sync")).status_code == 403


async def test_repo_unavailable_blocks_start_until_synced() -> None:
    w = World()
    w.repo.fail_ensure = True
    pid, _ = await _setup(w)  # 建仓库失败不挡住建项目
    lead, dev = await w.as_("lead"), await w.as_("dev")
    view = ok(await lead.get(f"/api/v1/projects/{pid}/repository"))
    assert view["status"] == "error" and "暂不可用" in view["error"]
    r = await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"})
    assert r.json()["code"] == "REPO_NOT_READY"
    w.repo.fail_ensure = False
    assert ok(await lead.post(f"/api/v1/projects/{pid}/repository/sync"))["status"] == "ready"
    ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)


async def test_submit_locks_commit_and_process_files_preview() -> None:
    """提交评审时锁定 commit（来源按尾注 / 绑定账号判定）；目录预览只开放本流程目录。"""
    w = World(oauth=_oauth())
    pid, _ = await _setup(w)
    dev, outsider = await w.as_("dev"), await w.as_("outsider")
    await _bind(w, dev, "c-dev")
    proc = ok(await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}), 201)["id"]
    await w.settle()
    n1 = await _node(dev, proc, "n1")
    repo = _repo(w, pid)

    # 王工在本地改了产物并推送 → 提交评审时锁定的是他的提交（来源 = 人工）
    w.repo.push(
        repo, {n1["artifact_path"]: "# 人工修订"}, "补充异常场景", author_login="wang-gitee"
    )
    w.repo.push(repo, {"README.md": "别的流程不该看到"}, "根目录文件")
    ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/submit-review"))
    a = (await _node(dev, proc, "n1"))["artifacts"][0]
    assert a["source"] == "user" and a["by"] == "王工" and a["author_login"] == "wang-gitee"
    assert (
        ok(await dev.get(f"/api/v1/processes/{proc}/nodes/n1/artifacts/1"))["content"]
        == "# 人工修订"
    )

    # 目录预览：默认 master；只列本流程目录
    tree = ok(await dev.get(f"/api/v1/processes/{proc}/files"))
    assert tree["root"] == "0001-A/" and tree["ref"] == w.repo.last(repo).sha
    assert [f["path"] for f in tree["files"]] == [n1["artifact_path"]]
    # 按锁定的 commit 读文件
    c = ok(
        await dev.get(
            f"/api/v1/processes/{proc}/files/content",
            params={"path": n1["artifact_path"], "ref": a["commit"]},
        )
    )
    assert c["content"] == "# 人工修订"
    # 流程目录以外不开放；不存在的 ref；非团队成员看不到
    r = await dev.get(f"/api/v1/processes/{proc}/files/content", params={"path": "README.md"})
    assert r.json()["code"] == "PATH_OUT_OF_PROCESS"
    r = await dev.get(f"/api/v1/processes/{proc}/files", params={"ref": "deadbeef"})
    assert r.json()["code"] == "REF_NOT_FOUND"
    assert (await outsider.get(f"/api/v1/processes/{proc}/files")).status_code == 404
