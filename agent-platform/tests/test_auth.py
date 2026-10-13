"""身份与鉴权（doc/auth-permission-design.html）：会话归属隔离、服务账号、用户中心登录。

★ 用户中心用 httpx.MockTransport 模拟，不碰真实服务。
★ 只在独立测试库上跑（DATABASE_URL 指向测试库）；用到的数据都带随机后缀。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from atlas_server.auth.service import snapshot_cache
from atlas_server.auth.uc import UCClient
from atlas_server.config import get_settings
from atlas_server.main import create_app
from httpx import ASGITransport

SPEC = {"system_prompt": "p", "model": {"model": "claude-opus-5"}, "tool_names": []}


class _NoopExecutor:
    """只接住 submit：这里测的是归属与发起人，不需要 run 真跑起来。"""

    async def submit(self, run_id: Any) -> None:
        return None


def _app() -> Any:
    app = create_app()
    app.state.executor = _NoopExecutor()
    return app


def _client(app: Any, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    )


async def _agent(c: httpx.AsyncClient, *, enable: bool = False) -> str:
    r = await c.post(
        "/v1/agents", json={"slug": f"a-{uuid.uuid4().hex[:8]}", "name": "测试", "spec": SPEC}
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    if enable:
        ok = await c.post(f"/v1/agents/{aid}/status", json={"status": "enabled"})
        assert ok.status_code == 200, ok.text
    return aid


# ───────────────────────────── dev 模式：会话按所有者隔离


async def test_threads_are_isolated_by_owner(clean_db: None) -> None:
    app = _app()
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    async with _client(app, **{"X-User-Id": alice}) as a, _client(app, **{"X-User-Id": bob}) as b:
        aid = await _agent(a)
        t = (await a.post("/v1/threads", json={"agent_id": aid, "title": "a 的会话"})).json()
        # bob 看不到、改不了、删不了、发不了消息、订阅不了 → 一律 404（不泄露存在）
        assert (await b.get(f"/v1/threads/{t['id']}")).status_code == 404
        assert (await b.patch(f"/v1/threads/{t['id']}", json={"title": "x"})).status_code == 404
        assert (await b.get(f"/v1/threads/{t['id']}/messages")).status_code == 404
        assert (await b.get(f"/v1/threads/{t['id']}/files")).status_code == 404
        r = await b.post(
            f"/v1/threads/{t['id']}/runs", json={"content": [{"type": "text", "text": "x"}]}
        )
        assert r.status_code == 404
        assert (await b.delete(f"/v1/threads/{t['id']}")).status_code == 404
        # 列表只有自己的
        assert [x["id"] for x in (await a.get("/v1/threads")).json()["data"]] == [t["id"]]
        assert (await b.get("/v1/threads")).json()["data"] == []
        assert (await a.get(f"/v1/threads/{t['id']}")).status_code == 200


# ───────────────────────────── 服务账号


async def test_service_account_threads_and_scope(clean_db: None) -> None:
    app = create_app()
    admin_id = str(uuid.uuid4())
    async with _client(app, **{"X-User-Id": admin_id}) as admin:  # dev：操作码全有
        enabled = await _agent(admin, enable=True)
        draft = await _agent(admin)
        r = await admin.post(
            "/v1/service-accounts",
            json={"name": f"teamflow-{uuid.uuid4().hex[:6]}", "allow_acting": True},
        )
        assert r.status_code == 201, r.text
        sa = r.json()
        assert sa["key"].startswith("atlas_sk_") and sa["memory_default"] == "off"
        # 列表里不再返回密钥
        listed = [
            x for x in (await admin.get("/v1/service-accounts")).json() if x["id"] == sa["id"]
        ]
        assert listed and listed[0]["key"] is None

    acting = str(uuid.uuid4())
    async with _client(
        app, Authorization=f"Bearer {sa['key']}", **{"X-Atlas-Acting-User": acting}
    ) as svc:
        # 只看得到可用范围内的（all_enabled：草稿不可见）
        ids = {a["id"] for a in (await svc.get("/v1/agents", params={"limit": 200})).json()["data"]}
        assert enabled in ids and draft not in ids
        assert (await svc.post("/v1/threads", json={"agent_id": draft})).status_code == 404
        t = (await svc.post("/v1/threads", json={"agent_id": enabled, "title": "节点"})).json()
        assert t["memory"] == "off"  # 服务账号的会话默认关闭用户记忆
        assert (await svc.get(f"/v1/threads/{t['id']}")).status_code == 200
        # 服务账号不能建 / 改智能体
        assert (
            await svc.post("/v1/agents", json={"slug": "x", "name": "x", "spec": SPEC})
        ).status_code == 403

    async with _client(app, **{"X-User-Id": admin_id}) as admin:
        # 服务账号的会话不属于任何用户
        assert (await admin.get(f"/v1/threads/{t['id']}")).status_code == 404
        # 停用后密钥失效
        r = await admin.patch(f"/v1/service-accounts/{sa['id']}", json={"status": "disabled"})
        assert r.json()["status"] == "disabled"
    async with _client(app, Authorization=f"Bearer {sa['key']}") as svc:
        assert (await svc.get("/v1/agents")).status_code == 401
    async with _client(app, Authorization="Bearer not-a-key") as bad:
        assert (await bad.get("/v1/agents")).status_code == 401


async def test_run_records_acting_user(clean_db: None) -> None:
    from atlas_server.db.models import Run
    from atlas_server.db.session import get_sessionmaker

    app = _app()
    async with _client(app, **{"X-User-Id": str(uuid.uuid4())}) as admin:
        aid = await _agent(admin, enable=True)
        sa = (
            await admin.post(
                "/v1/service-accounts",
                json={"name": f"svc-{uuid.uuid4().hex[:6]}", "allow_acting": True},
            )
        ).json()
    acting = uuid.uuid4()
    async with _client(
        app, Authorization=f"Bearer {sa['key']}", **{"X-Atlas-Acting-User": str(acting)}
    ) as svc:
        t = (await svc.post("/v1/threads", json={"agent_id": aid})).json()
        r = await svc.post(
            f"/v1/threads/{t['id']}/runs",
            json={"content": [{"type": "text", "text": "hi"}]},
            headers={"Idempotency-Key": "k1"},
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
    async with get_sessionmaker()() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None and run.created_by == acting


# ───────────────────────────── 用户中心登录（uc 模式）


class FakeUC:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}

    def add(self, account: str, ops: list[str], *, can_access: bool = True) -> str:
        uid = str(uuid.uuid4())
        self.users[account] = {
            "id": uid,
            "account": account,
            "name": account.upper(),
            "email": f"{account}@x.com",
            "status": "active",
            "dept": {"id": str(uuid.uuid4()), "path": "研发部"},
        }
        self.snapshots[uid] = {
            "can_access": can_access,
            "roles": ["ATLAS_MEMBER"],
            "permissions": ops,
            "menus": [{"name": "对话", "path": "/chat"}],
        }
        return uid

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/open/v1/auth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        assert req.headers.get("authorization") == "Bearer t"
        if path == "/open/v1/auth/verify-password":
            body = json.loads(req.content)
            u = self.users.get(body["account"])
            if u is None or body["password"] != "pw":
                return httpx.Response(
                    401, json={"code": "BAD_CREDENTIALS", "message": "账号或密码错误"}
                )
            return httpx.Response(200, json={"user": u, **self.snapshots[u["id"]]})
        uid = path.split("/")[4]
        if path.endswith("/authz"):
            return httpx.Response(200, json=self.snapshots[uid])
        u = next(x for x in self.users.values() if x["id"] == uid)
        return httpx.Response(200, json=u)


@pytest.fixture
def uc_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AUTH_MODE", "uc")
    monkeypatch.setenv("UC_APP_KEY", "ak")
    monkeypatch.setenv("CONFIG_INTERNAL_TOKEN", "internal-secret")
    get_settings.cache_clear()
    snapshot_cache.clear()
    yield
    get_settings.cache_clear()
    snapshot_cache.clear()


@pytest.fixture
async def uc_app(uc_mode: None) -> AsyncIterator[tuple[Any, FakeUC]]:
    fake = FakeUC()
    app = create_app()
    app.state.uc = UCClient(get_settings(), transport=httpx.MockTransport(fake.handler))
    yield app, fake
    await app.state.uc.aclose()


async def _login(app: Any, account: str) -> httpx.AsyncClient:
    c = _client(app)
    r = await c.post("/v1/auth/login", json={"account": account, "password": "pw"})
    assert r.status_code == 200, r.text
    c.headers["X-Atlas-CSRF"] = c.cookies["atlas_csrf"]
    return c


async def test_uc_login_csrf_and_me(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    app, fake = uc_app
    fake.add("zhang", ["chat:use", "agent:create"])
    fake.add("outsider", ["chat:use"], can_access=False)

    async with _client(app, **{"X-User-Id": str(uuid.uuid4())}) as anon:
        # uc 模式下请求头身份无效；没声明身份依赖的接口（模型目录）也不能匿名访问
        assert (await anon.get("/v1/threads")).status_code == 401
        assert (await anon.get("/v1/models")).status_code == 401
        r = await anon.post("/v1/auth/login", json={"account": "zhang", "password": "bad"})
        assert r.status_code == 401
        r = await anon.post("/v1/auth/login", json={"account": "outsider", "password": "pw"})
        assert r.status_code == 403  # 不在 Atlas 的可访问范围

    c = await _login(app, "zhang")
    me = (await c.get("/v1/me")).json()
    assert (
        me["account"] == "zhang" and me["dept"] == "研发部" and "agent:create" in me["operations"]
    )
    # 写请求必须带 CSRF 头
    csrf = c.headers.pop("X-Atlas-CSRF")
    r = await c.post("/v1/agents", json={"slug": "no-csrf", "name": "x", "spec": SPEC})
    assert r.status_code == 403
    c.headers["X-Atlas-CSRF"] = csrf
    aid = await _agent(c)
    t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
    assert t["memory"] == "on"
    # 登出后会话失效
    assert (await c.post("/v1/auth/logout")).status_code == 200
    assert (await c.get("/v1/me")).status_code == 401
    await c.aclose()


async def test_uc_operations_and_revocation(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    app, fake = uc_app
    fake.add("builder", ["chat:use", "agent:create"])
    member_id = fake.add("member", ["chat:use"])

    b = await _login(app, "builder")
    aid = await _agent(b)
    m = await _login(app, "member")
    # 成员不能建智能体，也不能改别人的智能体
    r = await m.post("/v1/agents", json={"slug": "m", "name": "m", "spec": SPEC})
    assert r.status_code == 403
    assert (await m.patch(f"/v1/agents/{aid}", json={"name": "改"})).status_code == 403
    # 创建者可以改
    assert (await b.patch(f"/v1/agents/{aid}", json={"name": "改"})).status_code == 200

    # 用户中心收回访问权 → 快照过期后会话被清除
    fake.snapshots[member_id]["can_access"] = False
    snapshot_cache.drop(uuid.UUID(member_id))
    assert (await m.get("/v1/me")).status_code == 401
    fake.snapshots[member_id]["can_access"] = True
    snapshot_cache.drop(uuid.UUID(member_id))
    assert (await m.get("/v1/me")).status_code == 401  # 会话已删，须重新登录
    await b.aclose()
    await m.aclose()


async def test_internal_introspect(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    app, fake = uc_app
    fake.add("reviewer", ["chat:use", "skill:review"])
    c = await _login(app, "reviewer")
    token = c.cookies["atlas_session"]
    async with _client(app) as internal:
        r = await internal.post("/internal/v1/auth/introspect", json={"token": token})
        assert r.status_code == 401  # 没有内部令牌
        r = await internal.post(
            "/internal/v1/auth/introspect",
            json={"token": token},
            headers={"X-Internal-Token": "internal-secret"},
        )
        assert r.status_code == 200 and "skill:review" in r.json()["operations"]
        r = await internal.post(
            "/internal/v1/auth/introspect",
            json={"token": "forged"},
            headers={"X-Internal-Token": "internal-secret"},
        )
        assert r.status_code == 401
    await c.aclose()


async def test_internal_introspect_service_key(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    """知识库服务的对外 API 收到服务账号密钥，也交给 introspect 校验。"""
    app, fake = uc_app
    fake.add("sa-admin", ["service_account:manage"])
    admin = await _login(app, "sa-admin")
    r = await admin.post(
        "/v1/service-accounts",
        json={"name": f"kb-{uuid.uuid4().hex[:6]}"},
        headers={"X-Atlas-CSRF": admin.cookies["atlas_csrf"]},
    )
    assert r.status_code == 201, r.text
    sa = r.json()
    async with _client(app) as internal:
        h = {"X-Internal-Token": "internal-secret"}
        r = await internal.post(
            "/internal/v1/auth/introspect", json={"service_key": sa["key"]}, headers=h
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "service" and r.json()["user_id"] == sa["id"]
        r = await internal.post(
            "/internal/v1/auth/introspect", json={"service_key": "atlas_sk_forged"}, headers=h
        )
        assert r.status_code == 401
        assert (
            await internal.post("/internal/v1/auth/introspect", json={}, headers=h)
        ).status_code == 401
    await admin.aclose()


async def test_thread_delete_removes_dependent_rows(clean_db: None) -> None:
    """0013 去掉外键后没有级联：删会话要把 message / run 一起删掉，不留孤儿。"""
    from atlas_server.db.models import Message, Run
    from atlas_server.db.session import get_sessionmaker
    from sqlalchemy import func, select

    app = _app()
    async with _client(app, **{"X-User-Id": str(uuid.uuid4())}) as c:
        aid = await _agent(c)
        t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
        r = await c.post(
            f"/v1/threads/{t['id']}/runs", json={"content": [{"type": "text", "text": "hi"}]}
        )
        assert r.status_code == 202
        assert (await c.delete(f"/v1/threads/{t['id']}")).status_code == 204
    tid = uuid.UUID(t["id"])
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(func.count()).where(Message.thread_id == tid)) == 0
        assert await s.scalar(select(func.count()).where(Run.thread_id == tid)) == 0
