"""多实例：Redis 协调（唤醒广播、节点租约锁、崩溃接管）与 Gitee 仓库客户端。

★ Redis 用例需要本机 Redis（docker compose 的 6380）；不可用时跳过。频道 / 键用随机前缀隔离。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import httpx
import pytest
from atlas_teamflow.agents.supervisor import AgentSupervisor
from atlas_teamflow.db.models import NodeMessage, ProcessNode
from atlas_teamflow.db.session import get_sessionmaker
from atlas_teamflow.process.bus import RedisCoordinator
from atlas_teamflow.process.repo_host import GiteeHost
from sqlalchemy import func, select
from test_tf_process import _node, _setup
from tf_testkit import World, ok

REDIS = "redis://localhost:6380/15"


async def _redis_ok() -> bool:
    import redis.asyncio as aioredis

    c = aioredis.from_url(REDIS)
    try:
        return bool(await asyncio.wait_for(c.ping(), 1))
    except Exception:
        return False
    finally:
        await c.aclose()


def _coord(prefix: str, lease: int = 30) -> RedisCoordinator:
    return RedisCoordinator(REDIS, lease_seconds=lease, prefix=prefix)


async def _wait(cond: Any, seconds: float = 15) -> None:
    for _ in range(int(seconds * 10)):
        if await cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("等待超时")


async def _agent_messages(proc: str) -> int:
    async with get_sessionmaker()() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(NodeMessage)
                .join(ProcessNode, ProcessNode.uuid == NodeMessage.node_uuid)
                .where(ProcessNode.process_uuid == uuid.UUID(proc), NodeMessage.role == "agent")
            )
            or 0
        )


async def test_two_instances_process_each_node_once() -> None:
    """两个实例都在跑监督器：唤醒广播到两边，但每个节点只被一个实例处理（Agent 只收到一次开工）。"""
    if not await _redis_ok():
        pytest.skip("本机 Redis 不可用")
    prefix = f"tftest:{uuid.uuid4().hex[:8]}:"
    a = _coord(prefix)
    w = World(coord=a, redis_url=REDIS)
    b = _coord(prefix)
    sup_b = AgentSupervisor(
        get_sessionmaker,
        w.atlas,  # type: ignore[arg-type]
        w.app.state.repo,
        coord=b,
        sweep_seconds=0.3,
        git_profile="teamflow-gitee",
    )
    sup_a = w.app.state.supervisor
    sup_a.sweep_seconds = 0.3
    await a.start()
    await b.start()
    await sup_a.start()
    await sup_b.start()
    try:
        pid, _ = await _setup(w)
        dev = await w.as_("dev")
        proc = ok(
            await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}),
            201,
        )["id"]
        # 没有上游的节点等执行人第一次说明才启动 Agent
        ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "开始"}), 201)

        async def has_artifact() -> bool:
            return await _replies(dev, proc) >= 1

        await _wait(has_artifact)
        await asyncio.sleep(1)  # 让两边的扫描再各跑几轮，确认不会重复处理
        await sup_a.idle()
        await sup_b.idle()
        assert sum(len(v) for v in w.atlas.inputs.values()) == 1
        assert len(w.atlas.threads) == 1
        assert await _agent_messages(proc) == 1
        assert await _replies(dev, proc) == 1

        # 跨实例实时推送：订阅在 B 上，也能收到 A 上发生的事件通知
        q = b.subscribe(uuid.UUID(proc))
        ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "补充"}), 201)
        msg = await asyncio.wait_for(q.get(), 5)
        assert msg["kind"] in ("nudge", "delta")
        b.unsubscribe(uuid.UUID(proc), q)
        await _wait(lambda: _has_replies(dev, proc, 2))
    finally:
        await sup_a.close()
        await sup_b.close()
        await a.close()
        await b.close()


async def _replies(dev: Any, proc: str) -> int:
    """n1 上 Agent 已完成的回复数（TeamFlow 不看仓库：以运行结束落库为准）。"""
    msgs = (await _node(dev, proc, "n1"))["messages"]
    return sum(1 for m in msgs if m["role"] == "agent" and m["status"] == "done")


async def _has_replies(dev: Any, proc: str, n: int) -> bool:
    return await _replies(dev, proc) >= n


async def test_crashed_instance_is_taken_over_after_lease_expires() -> None:
    """API 不跑监督器（只广播唤醒）；节点租约被一个已经死掉的实例占着 → 过期后由 worker 接手。"""
    if not await _redis_ok():
        pytest.skip("本机 Redis 不可用")
    prefix = f"tftest:{uuid.uuid4().hex[:8]}:"
    api_coord = _coord(prefix)
    w = World(coord=api_coord, redis_url=REDIS, embedded_worker=False)
    assert w.app.state.supervisor is None
    await api_coord.start()
    worker_coord = _coord(prefix, lease=3)
    try:
        pid, _ = await _setup(w)
        dev = await w.as_("dev")
        proc = ok(
            await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A"}),
            201,
        )["id"]
        # 没有上游的节点等执行人第一次说明才启动 Agent
        ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "开始"}), 201)
        async with get_sessionmaker()() as s:
            node_uuid = await s.scalar(
                select(ProcessNode.uuid).where(
                    ProcessNode.process_uuid == uuid.UUID(proc), ProcessNode.node_id == "n1"
                )
            )
        # 一个「已经死掉的实例」占着租约，1.5 秒后过期
        await worker_coord.redis.set(worker_coord.LOCK + str(node_uuid), "dead", px=1500)
        await worker_coord.start()
        sup = AgentSupervisor(
            get_sessionmaker,
            w.atlas,  # type: ignore[arg-type]
            w.app.state.repo,
            coord=worker_coord,
            lease_seconds=3,
            sweep_seconds=0.3,
            git_profile="teamflow-gitee",
        )
        await sup.start()
        await asyncio.sleep(0.8)
        assert await _replies(dev, proc) == 0  # 租约还没过期：没人处理

        async def taken_over() -> bool:
            return await _replies(dev, proc) >= 1

        await _wait(taken_over, 10)
        await sup.close()
    finally:
        await api_coord.close()
        await worker_coord.close()


class FakeGitee:
    """Gitee v5 的最小模拟（行为按实测：重复建仓库 422；协作者权限在 permissions 里）。"""

    def __init__(self) -> None:
        self.repos: set[str] = set()
        self.collab: dict[str, str] = {"me": "admin"}
        self.auth: set[str] = set()
        self.queries: list[dict[str, str]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.auth.add(req.headers.get("authorization", ""))
        assert "access_token" not in str(req.url)  # token 只在请求头里
        path = req.url.path.removeprefix("/api/v5")
        if path == "/user":
            return httpx.Response(200, json={"login": "me"})
        if path == "/user/repos" and req.method == "POST":
            name = json.loads(req.content)["name"]
            if name in self.repos:
                return httpx.Response(422, json={"error": "exists"})
            self.repos.add(name)
            return httpx.Response(201, json={"name": name})
        parts = path.split("/")  # ["", "repos", owner, repo, ...]
        repo, rest = parts[3], parts[4:]
        if not rest:
            return httpx.Response(200 if repo in self.repos else 404, json={})
        if rest == ["commits"]:
            self.queries.append(dict(req.url.params))
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "c2",
                        "commit": {
                            "message": "改需求\n\nTeamFlow-Node: n1",
                            "author": {
                                "name": "Agent",
                                "email": "a@x",
                                "date": "2026-10-10T08:00:00Z",
                            },
                        },
                        "author": None,
                    },
                    {
                        "sha": "c1",
                        "commit": {
                            "message": "手写",
                            "author": {
                                "name": "王工",
                                "email": "w@x",
                                "date": "2026-10-10T07:00:00+08:00",
                            },
                        },
                        "author": {"login": "wang"},
                    },
                ],
            )
        if rest[:1] == ["commits"] and len(rest) == 2:  # 分支名 / sha → commit
            sha = {"master": "c2", "c1": "c1", "c2": "c2"}.get(rest[1])
            return httpx.Response(200, json={"sha": sha}) if sha else httpx.Response(404, json={})
        if rest == ["git", "trees", "c2"]:
            assert req.url.params.get("recursive") == "1"
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "0001-A", "type": "tree"},
                        {"path": "0001-A/01-需求/需求.md", "type": "blob", "size": 4},
                        {"path": "0002-B/01-需求/需求.md", "type": "blob", "size": 9},
                    ],
                    "truncated": False,
                },
            )
        if rest[0] == "contents":
            if req.url.params.get("ref") != "c2":
                return httpx.Response(200, json=[])  # 缺文件：200 []
            return httpx.Response(200, json={"content": base64.b64encode(b"# v2").decode()})
        if rest == ["collaborators"]:
            return httpx.Response(
                200,
                json=[
                    {
                        "login": k,
                        "permissions": {"admin": v == "admin", "push": v != "pull", "pull": True},
                    }
                    for k, v in self.collab.items()
                ],
            )
        if rest[0] == "collaborators":
            if req.method == "PUT":
                self.collab[rest[1]] = json.loads(req.content)["permission"]
            else:
                self.collab.pop(rest[1], None)
            return httpx.Response(204)
        return httpx.Response(404, json={})


async def test_gitee_host_reads_commits_and_manages_collaborators() -> None:
    fake = FakeGitee()
    host = GiteeHost(
        api="https://gitee.com/api/v5", token="t0k", transport=httpx.MockTransport(fake.handler)
    )
    project = uuid.uuid4()
    name = host.repo_name(project)
    info = await host.ensure_repo(name, "x")
    await host.ensure_repo(name, "x")  # 已建好：不重复建
    assert fake.repos == {f"teamflow-{project.hex}"}
    assert info.clone_url == f"https://gitee.com/me/{name}.git"

    c2, c1 = await host.commits(name, path="0001-A/01-需求/需求.md")
    assert fake.queries[-1] == {"per_page": "20", "path": "0001-A/01-需求/需求.md"}
    assert c2.sha == "c2" and c2.author_login is None and c2.web_url.endswith("/commit/c2")
    assert c1.author_login == "wang" and c1.committed_at.utcoffset() is not None
    assert await host.resolve(name, "master") == "c2" and await host.resolve(name, "nope") is None
    files, truncated = await host.tree(name, "c2", "0001-A/")
    assert [(f.path, f.size) for f in files] == [("0001-A/01-需求/需求.md", 4)] and not truncated
    assert await host.read_file(name, "0001-A/01-需求/需求.md", "c2") == "# v2"
    assert await host.read_file(name, "0001-A/01-需求/需求.md", "c1") is None

    await host.set_collaborator(name, "wang", "pull")
    assert await host.collaborators(name) == {"me": "admin", "wang": "pull"}
    await host.remove_collaborator(name, "wang")
    assert await host.collaborators(name) == {"me": "admin"}
    assert fake.auth == {"token t0k"}
    await host.aclose()
