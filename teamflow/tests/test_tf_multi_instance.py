"""多实例：Redis 协调（唤醒广播、节点租约锁、崩溃接管）与 Gitee 产物存储。

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
from atlas_teamflow.process.git_store import GiteeStore, LocalGitStore
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
    sup_b = AgentSupervisor(get_sessionmaker, w.atlas, w.app.state.git, coord=b, sweep_seconds=0.3)  # type: ignore[arg-type]
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
            await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A", "requirement": "B"}),
            201,
        )["id"]

        async def has_artifact() -> bool:
            return bool((await _node(dev, proc, "n1"))["artifacts"])

        await _wait(has_artifact)
        await asyncio.sleep(1)  # 让两边的扫描再各跑几轮，确认不会重复处理
        await sup_a.idle()
        await sup_b.idle()
        assert sum(len(v) for v in w.atlas.inputs.values()) == 1
        assert len(w.atlas.threads) == 1
        assert await _agent_messages(proc) == 1
        n1 = await _node(dev, proc, "n1")
        assert [a["version"] for a in n1["artifacts"]] == [1]

        # 跨实例实时推送：订阅在 B 上，也能收到 A 上发生的事件通知
        q = b.subscribe(uuid.UUID(proc))
        ok(await dev.post(f"/api/v1/processes/{proc}/nodes/n1/messages", {"text": "补充"}), 201)
        msg = await asyncio.wait_for(q.get(), 5)
        assert msg["kind"] in ("nudge", "delta")
        b.unsubscribe(uuid.UUID(proc), q)
        await _wait(lambda: _versions(dev, proc, 2))
    finally:
        await sup_a.close()
        await sup_b.close()
        await a.close()
        await b.close()


async def _versions(dev: Any, proc: str, n: int) -> bool:
    return len((await _node(dev, proc, "n1"))["artifacts"]) >= n


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
            await dev.post(f"/api/v1/projects/{pid}/processes", {"title": "A", "requirement": "B"}),
            201,
        )["id"]
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
            w.atlas,
            w.app.state.git,
            coord=worker_coord,
            lease_seconds=3,
            sweep_seconds=0.3,
        )  # type: ignore[arg-type]
        await sup.start()
        await asyncio.sleep(0.8)
        assert not (await _node(dev, proc, "n1"))["artifacts"]  # 租约还没过期：没人处理

        async def taken_over() -> bool:
            return bool((await _node(dev, proc, "n1"))["artifacts"])

        await _wait(taken_over, 10)
        await sup.close()
    finally:
        await api_coord.close()
        await worker_coord.close()


class FakeGitee:
    """Gitee v5 contents API 的最小模拟（行为按实测：缺文件返回 200 []、重复建仓库 422）。"""

    def __init__(self) -> None:
        self.repos: set[str] = set()
        self.files: dict[
            tuple[str, str], list[tuple[str, str]]
        ] = {}  # (repo, path) → [(commit, content)]
        self.auth: set[str] = set()

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
        parts = path.split("/")
        repo = parts[3]
        if len(parts) == 4:
            return httpx.Response(200 if repo in self.repos else 404, json={})
        fpath = httpx.URL("/" + "/".join(parts[5:])).path.lstrip("/")
        key = (repo, fpath)
        hist = self.files.get(key, [])
        if req.method == "GET":
            ref = req.url.params.get("ref")
            hit = (
                next((c for c in hist if c[0] == ref), None)
                if ref
                else (hist[-1] if hist else None)
            )
            if not hit:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json={
                    "sha": "blob-" + hit[0],
                    "content": base64.b64encode(hit[1].encode()).decode(),
                },
            )
        body = json.loads(req.content)
        if req.method == "PUT":
            assert hist and body["sha"] == "blob-" + hist[-1][0]
        else:
            assert not hist
        commit = uuid.uuid4().hex
        self.files.setdefault(key, []).append((commit, base64.b64decode(body["content"]).decode()))
        return httpx.Response(
            201 if req.method == "POST" else 200, json={"commit": {"sha": commit}}
        )


async def test_gitee_store_commit_and_read() -> None:
    fake = FakeGitee()
    store = GiteeStore(
        api="https://gitee.com/api/v5", token="t0k", transport=httpx.MockTransport(fake.handler)
    )
    team, project = uuid.uuid4(), uuid.uuid4()
    path = "0001-支持手机号登录/01-需求分析/需求文档.md"
    c1 = await store.commit(team, project, path, "# v1", "v1")
    c2 = await store.commit(team, project, path, "# v2", "v2")
    assert c1 != c2 and fake.repos == {f"teamflow-{project.hex}"}
    assert await store.read(team, project, c1, path) == "# v1\n"
    assert await store.read(team, project, c2, path) == "# v2\n"
    assert fake.auth == {"token t0k"}
    url = store.web_url(team, project, c2, path)
    assert url and url.startswith(f"https://gitee.com/me/teamflow-{project.hex}/blob/{c2}/")
    await store.aclose()


async def test_local_store_still_works(tmp_path: Any) -> None:
    store = LocalGitStore(tmp_path, "T", "t@x")
    team, project = uuid.uuid4(), uuid.uuid4()
    sha = await store.commit(team, project, "a/b.md", "x", "m")
    assert await store.read(team, project, sha, "a/b.md") == "x\n"
    assert store.web_url(team, project, sha, "a/b.md") is None
