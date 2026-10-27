"""身份与鉴权（doc/auth-permission-design.html、doc/identity-propagation-design.html）。

会话归属隔离、业务应用建的会话（source_app）、请求头身份（按 auth-uid 取用户中心授权快照）。

★ 用户中心用 httpx.MockTransport 模拟，不碰真实服务。
★ 只在独立测试库上跑（DATABASE_URL 指向测试库）；用到的数据都带随机后缀。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from atlas_agent.auth.service import snapshot_cache
from atlas_agent.auth.uc import UCClient
from atlas_agent.config import get_settings
from atlas_agent.main import create_app
from httpx import ASGITransport
from sqlalchemy import select

from tests.fake_uc import FakeUC

SPEC = {"system_prompt": "p", "model": {"model": "claude-opus-5"}, "tool_names": []}
TEXT = {"content": [{"type": "text", "text": "hi"}]}


class _NoopExecutor:
    """只接住 submit：这里测的是归属与发起人，不需要 run 真跑起来。"""

    async def submit(self, run_id: Any) -> None:
        return None


def _app() -> Any:
    app = create_app()
    app.state.executor = _NoopExecutor()
    return app


def _client(app: Any, uid: str | None = None, caller: str = "gateway", **headers: str) -> Any:
    h = {"X-Caller-App": caller, **headers}
    if uid:
        h["X-Uid"] = uid
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=h)


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


# ───────────────────────────── 会话按使用者（X-Uid）隔离


async def test_threads_are_isolated_by_owner(clean_db: None) -> None:
    app = _app()
    alice, bob = str(uuid.uuid4()), str(uuid.uuid4())
    async with _client(app, alice) as a, _client(app, bob) as b:
        aid = await _agent(a)
        t = (await a.post("/v1/threads", json={"agent_id": aid, "title": "a 的会话"})).json()
        assert t["source_app"] is None and t["memory"] == "on"
        # bob 看不到、改不了、删不了、发不了消息、订阅不了 → 一律 404（不泄露存在）
        assert (await b.get(f"/v1/threads/{t['id']}")).status_code == 404
        assert (await b.patch(f"/v1/threads/{t['id']}", json={"title": "x"})).status_code == 404
        assert (await b.get(f"/v1/threads/{t['id']}/messages")).status_code == 404
        assert (await b.get(f"/v1/threads/{t['id']}/files")).status_code == 404
        assert (await b.post(f"/v1/threads/{t['id']}/runs", json=TEXT)).status_code == 404
        assert (await b.delete(f"/v1/threads/{t['id']}")).status_code == 404
        # 列表只有自己的
        assert [x["id"] for x in (await a.get("/v1/threads")).json()["data"]] == [t["id"]]
        assert (await b.get("/v1/threads")).json()["data"] == []
        assert (await a.get(f"/v1/threads/{t['id']}")).status_code == 200


# ───────────────────────────── 业务应用建的会话（内网身份传递 §7.3）


async def test_business_app_threads(clean_db: None) -> None:
    """TeamFlow 建的会话：归 X-Uid 的人；同一应用来访时信任它已授权（团队其他执行人也能经它
    看、能发指令）；从网关来只读；别的应用看不到。"""
    from atlas_agent.db.models import Run
    from atlas_agent.db.session import get_sessionmaker

    app = _app()
    li, wang = str(uuid.uuid4()), str(uuid.uuid4())
    async with _client(app, li) as admin:
        aid = await _agent(admin, enable=True)

    async with _client(app, li, "teamflow") as tf_li, _client(app, wang, "teamflow") as tf_wang:
        t = (await tf_li.post("/v1/threads", json={"agent_id": aid, "title": "节点"})).json()
        assert t["source_app"] == "teamflow"
        assert t["memory"] == "off"  # 业务应用建的会话默认关闭用户记忆
        # 团队里另一个执行人经 TeamFlow：能看、能发指令（TeamFlow 已做过执行角色校验）
        assert (await tf_wang.get(f"/v1/threads/{t['id']}")).status_code == 200
        r = await tf_wang.post(
            f"/v1/threads/{t['id']}/runs", json=TEXT, headers={"Idempotency-Key": "k1"}
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert (await tf_wang.get(f"/v1/runs/{run_id}")).status_code == 200
        # 应用可以显式打开记忆（评测的跨会话记忆用例）
        on = await tf_li.post("/v1/threads", json={"agent_id": aid, "memory": "on"})
        assert on.json()["memory"] == "on"

    async with _client(app, li) as gw_li, _client(app, wang) as gw_wang:
        # 发起人在 Atlas 里能看到（列表、详情），但只读：续聊要回到 TeamFlow
        assert t["id"] in [x["id"] for x in (await gw_li.get("/v1/threads")).json()["data"]]
        assert (await gw_li.get(f"/v1/threads/{t['id']}/messages")).status_code == 200
        r = await gw_li.post(f"/v1/threads/{t['id']}/runs", json=TEXT)
        assert r.status_code == 403
        assert (await gw_li.patch(f"/v1/threads/{t['id']}", json={"title": "x"})).status_code == 403
        assert (await gw_li.post(f"/v1/runs/{run_id}/cancel")).status_code == 403
        # 不是发起人：从网关来看不到
        assert (await gw_wang.get(f"/v1/threads/{t['id']}")).status_code == 404
    async with _client(app, wang, "bench") as bench:
        assert (await bench.get(f"/v1/threads/{t['id']}")).status_code == 404

    async with get_sessionmaker()() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None and str(run.created_by) == wang and str(run.auth_uid) == wang


async def test_run_records_auth_uid(clean_db: None) -> None:
    """run 记下「按谁判」（X-Auth-Uid，缺省 = X-Uid）：执行时一切出站调用都带它。"""
    from atlas_agent.db.models import Run
    from atlas_agent.db.session import get_sessionmaker

    app = _app()
    me, de = str(uuid.uuid4()), str(uuid.uuid4())
    async with _client(app, me, **{"X-Auth-Uid": de}) as c:
        aid = await _agent(c, enable=True)
        t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
        r = await c.post(f"/v1/threads/{t['id']}/runs", json=TEXT)
        assert r.status_code == 202, r.text
    async with get_sessionmaker()() as s:
        run = await s.get(Run, uuid.UUID(r.json()["run_id"]))
        assert run is not None and str(run.created_by) == me and str(run.auth_uid) == de


async def test_thread_delete_removes_dependent_rows(clean_db: None) -> None:
    """0013 去掉外键后没有级联：删会话要把 message / run 一起删掉，不留孤儿。"""
    from atlas_agent.db.models import Message, Run
    from atlas_agent.db.session import get_sessionmaker
    from sqlalchemy import func, select

    app = _app()
    async with _client(app, str(uuid.uuid4())) as c:
        aid = await _agent(c)
        t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
        assert (await c.post(f"/v1/threads/{t['id']}/runs", json=TEXT)).status_code == 202
        assert (await c.delete(f"/v1/threads/{t['id']}")).status_code == 204
    tid = uuid.UUID(t["id"])
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(func.count()).where(Message.thread_id == tid)) == 0
        assert await s.scalar(select(func.count()).where(Run.thread_id == tid)) == 0


# ───────────────────────────── 请求头身份 + 用户中心授权快照（登记具体的人测权限）


@pytest.fixture
async def uc_app() -> AsyncIterator[tuple[Any, FakeUC]]:
    fake = FakeUC()
    app = _app()
    app.state.uc = UCClient(get_settings(), transport=httpx.MockTransport(fake.handler))
    yield app, fake
    await app.state.uc.aclose()


async def test_uc_identity_headers_and_me(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    app, fake = uc_app
    zhang = fake.add("zhang", ["chat:use", "agent:create"])
    outsider = fake.add("outsider", ["chat:use"], can_access=False)
    # 数据权限领域：张对全部智能体只读（用户中心的数据编码级授权，与角色无关）
    fake.data_levels[zhang] = {"AtlasAgent": "READ"}

    async with _client(app) as anon:
        # 没有 X-Uid（没经过网关）：一律 401；没声明身份依赖的接口（模型目录）也一样
        assert (await anon.get("/v1/threads")).status_code == 401
        assert (await anon.get("/v1/models")).status_code == 401
        # 旧的请求头身份不再认
        assert (await anon.get("/v1/me", headers={"X-User-Id": zhang})).status_code == 401
    async with _client(app, outsider) as o:
        assert (await o.get("/v1/me")).status_code == 403  # 不在 Atlas 的可访问范围

    tenant = str(uuid.uuid4())
    async with _client(app, zhang, **{"X-Tenant-Id": tenant}) as c:
        me = (await c.get("/v1/me")).json()
        assert me["account"] == "zhang" and me["dept"] == "研发部"
        assert "agent:create" in me["operations"]
        assert me["data_levels"] == {
            "AtlasAgent": "READ",
            "AtlasKnowledgeBase": "NONE",
        }
        # 透传身份：用户中心收到的是这次请求的 uid 与租户
        assert fake.seen[-1] == {"x-uid": zhang, "x-auth-uid": zhang, "x-tenant-id": tenant}
        # 写请求不再要 CSRF 头（网关 + 用户中心 /auth/verify 统一校验）
        aid = await _agent(c)
        t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
        assert t["memory"] == "on"


async def test_uc_operations_follow_auth_uid(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    """操作码按 X-Auth-Uid 判（内网身份传递 §6）；用户中心收回访问权 → 快照过期后 403。"""
    app, fake = uc_app
    builder = fake.add("builder", ["chat:use", "agent:create"])
    member = fake.add("member", ["chat:use"])

    async with _client(app, builder) as b, _client(app, member) as m:
        aid = await _agent(b)
        # 成员不能建智能体；别人的草稿看不到（AtlasAgent 无权 → 404，不泄露存在）
        r = await m.post("/v1/agents", json={"slug": "m", "name": "m", "spec": SPEC})
        assert r.status_code == 403
        assert (await m.patch(f"/v1/agents/{aid}", json={"name": "改"})).status_code == 404
        # 创建者可以改
        assert (await b.patch(f"/v1/agents/{aid}", json={"name": "改"})).status_code == 200

    # 谁在用 = 成员，按谁判 = 构建者：操作码看构建者
    async with _client(app, member, **{"X-Auth-Uid": builder}) as on_behalf:
        r = await on_behalf.post(
            "/v1/agents", json={"slug": "on-behalf", "name": "ob", "spec": SPEC}
        )
        assert r.status_code == 201, r.text

    fake.snapshots[member]["can_access"] = False
    snapshot_cache.drop(uuid.UUID(member))
    async with _client(app, member) as m:
        assert (await m.get("/v1/me")).status_code == 403


async def test_internal_principals_without_token(
    uc_app: tuple[Any, FakeUC], clean_db: None
) -> None:
    """/internal 不再要服务间令牌（网关挡掉外部访问）；身份请求头原样透传给用户中心。"""
    app, fake = uc_app
    zhang = fake.add("zhang", ["chat:use"])
    async with _client(app, caller="atlas", **{"X-Uid": zhang, "X-Tenant-Id": "t-1"}) as kb:
        r = await kb.post("/internal/v1/auth/principals", json={"user_id": zhang})
        assert r.status_code == 200, r.text
        assert f"u:{zhang}" in r.json()["principals"] and "r:ATLAS_MEMBER" in r.json()["principals"]
        assert fake.seen[-1]["x-tenant-id"] == "t-1"


# ───────────────────────────── 数字员工与权限类型（doc/digital-employee-design.html）


async def _run_identity(run_id: str) -> tuple[str | None, str | None, str | None]:
    from atlas_agent.db.models import Run
    from atlas_agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        return str(run.created_by), str(run.auth_uid), run.auth_subject


async def test_agent_sharing_gates_use(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    """人 → Agent（§8.1）：建 Agent 登记创建者 = OWNER；没共享的人看不到、用不了；
    共享 READ 后能用不能改；收回后已有会话也发不了消息。"""
    app, fake = uc_app
    owner = fake.add("owner", ["chat:use", "agent:create"])
    member = fake.add("member", ["chat:use"])
    async with _client(app, owner) as o, _client(app, member) as m:
        aid = await _agent(o, enable=True)
        acl = fake.acl[("AtlasAgent", aid)]
        assert [(a["subject_id"], a["level"]) for a in acl] == [(owner, "OWNER")]
        assert (await o.get(f"/v1/agents/{aid}")).json()["my_level"] == "OWNER"

        assert aid not in [a["id"] for a in (await m.get("/v1/agents")).json()["data"]]
        assert (await m.get(f"/v1/agents/{aid}")).status_code == 404
        assert (await m.post("/v1/threads", json={"agent_id": aid})).status_code == 404

        r = await o.post(
            f"/v1/agents/{aid}/grants",
            json={"level": "READ", "subjects": [{"type": "user", "id": member}]},
        )
        assert r.status_code == 201, r.text
        grants = (await o.get(f"/v1/agents/{aid}/grants")).json()
        assert grants["my_level"] == "OWNER" and len(grants["data"]) == 2
        assert aid in [a["id"] for a in (await m.get("/v1/agents")).json()["data"]]
        t = (await m.post("/v1/threads", json={"agent_id": aid})).json()
        assert (await m.post(f"/v1/threads/{t['id']}/runs", json=TEXT)).status_code == 202
        # 能用不能改、不能共享
        assert (await m.patch(f"/v1/agents/{aid}", json={"name": "改"})).status_code == 403
        r = await m.post(
            f"/v1/agents/{aid}/grants",
            json={"level": "OWNER", "subjects": [{"type": "user", "id": member}]},
        )
        assert r.status_code == 403
        # 收回：已有会话也不能接着用
        member_acl = next(a for a in grants["data"] if a["subject"]["id"] == member)
        r = await o.delete(f"/v1/agents/{aid}/grants/{member_acl['id']}")
        assert r.status_code == 204
        r = await m.post(f"/v1/threads/{t['id']}/runs", json=TEXT)
        assert r.status_code == 403


async def test_auth_subject_decides_auth_uid(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    """权限类型（§7.1）：以人为权限 X-Auth-Uid = 使用者；以 Agent 为权限 = 数字员工，
    谁来用都一样。两种都建数字员工。"""
    app, fake = uc_app
    owner = fake.add("owner", ["chat:use", "agent:create"])
    member = fake.add("member", ["chat:use"])
    async with _client(app, owner) as o, _client(app, member) as m:
        as_user = await _agent(o, enable=True)
        r = await o.post(
            "/v1/agents",
            json={
                "slug": f"a-{uuid.uuid4().hex[:8]}",
                "name": "HR 助手",
                "spec": SPEC,
                "auth_subject": "agent",
            },
        )
        assert r.status_code == 201, r.text
        as_agent = r.json()["id"]
        de = r.json()["digital_employee_uid"]
        assert r.json()["auth_subject"] == "agent" and de in fake.digital
        assert fake.digital[de]["name"] == "🤖 HR 助手" and fake.digital[de]["owner_id"] == owner
        assert (await o.get(f"/v1/agents/{as_user}")).json()["digital_employee_uid"]
        assert (
            await o.post(f"/v1/agents/{as_agent}/status", json={"status": "enabled"})
        ).is_success
        for aid in (as_user, as_agent):
            fake.share("AtlasAgent", aid, member, "READ")

        async def run_as(c: httpx.AsyncClient, aid: str) -> str:
            t = (await c.post("/v1/threads", json={"agent_id": aid})).json()
            r = await c.post(f"/v1/threads/{t['id']}/runs", json=TEXT)
            assert r.status_code == 202, r.text
            return r.json()["run_id"]

        assert await _run_identity(await run_as(m, as_user)) == (member, member, "user")
        assert await _run_identity(await run_as(m, as_agent)) == (member, de, "agent")
        assert await _run_identity(await run_as(o, as_agent)) == (owner, de, "agent")

        # 身份页签：能用的人都能看
        ident = (await m.get(f"/v1/agents/{as_agent}/identity")).json()
        assert ident["auth_subject"] == "agent" and ident["digital_employee"]["id"] == de

        # 切换权限类型只有 OWNER；以 Agent 为权限的编辑也只有 OWNER
        fake.share("AtlasAgent", as_agent, member, "WRITE")
        fake.share("AtlasAgent", as_user, member, "WRITE")
        assert (await m.patch(f"/v1/agents/{as_user}", json={"name": "改"})).status_code == 200
        assert (await m.patch(f"/v1/agents/{as_agent}", json={"name": "改"})).status_code == 403
        r = await m.put(f"/v1/agents/{as_agent}/auth-subject", json={"auth_subject": "user"})
        assert r.status_code == 403
        r = await o.put(f"/v1/agents/{as_agent}/auth-subject", json={"auth_subject": "user"})
        assert r.status_code == 200 and r.json()["auth_subject"] == "user"
        assert await _run_identity(await run_as(m, as_agent)) == (member, member, "user")


async def test_digital_employee_follows_agent(uc_app: tuple[Any, FakeUC], clean_db: None) -> None:
    """改名同步、归档停用、恢复启用（§5.2）。"""
    app, fake = uc_app
    owner = fake.add("owner", ["chat:use", "agent:create"])
    async with _client(app, owner) as o:
        aid = await _agent(o)
        de = (await o.get(f"/v1/agents/{aid}")).json()["digital_employee_uid"]
        assert (await o.patch(f"/v1/agents/{aid}", json={"name": "新名字"})).status_code == 200
        assert fake.digital[de]["name"] == "🤖 新名字"
        assert (await o.delete(f"/v1/agents/{aid}")).status_code == 200
        assert fake.digital[de]["status"] == "disabled"
        r = await o.post(f"/v1/agents/{aid}/status", json={"status": "enabled"})
        assert r.status_code == 200 and fake.digital[de]["status"] == "active"


async def test_backfill_existing_agents(clean_db: None) -> None:
    """存量回填（§12）：补数字员工；创建者 = OWNER，已启用的根部门含下级 READ（保持人人可用）；
    已登记的不动；可重复执行。"""
    from atlas_agent.auth.backfill import backfill_agent_identity
    from atlas_agent.db.models import Agent
    from atlas_agent.db.session import get_sessionmaker

    fake = FakeUC()
    owner = fake.add("owner", ["chat:use", "agent:create"])
    # 迁移前建的：没有数字员工、没在用户中心登记
    async with get_sessionmaker()() as s:
        enabled = Agent(
            slug=f"e-{uuid.uuid4().hex[:8]}",
            name="已启用",
            status="enabled",
            created_by=uuid.UUID(owner),
        )
        draft = Agent(
            slug=f"d-{uuid.uuid4().hex[:8]}",
            name="草稿",
            status="draft",
            created_by=uuid.UUID(owner),
        )
        s.add_all([enabled, draft])
        await s.commit()
        ids = (str(enabled.id), str(draft.id))

    uc = UCClient(get_settings(), transport=httpx.MockTransport(fake.handler))
    try:
        out = await backfill_agent_identity(uc)
        assert out["failed"] == [] and out["digital_created"] == 2 and out["registered"] == 2
        e_acl = {
            (a["subject_type"], a["subject_id"], a["level"])
            for a in fake.acl[("AtlasAgent", ids[0])]
        }
        assert e_acl == {("USER", owner, "OWNER"), ("DEPT", fake.root_dept, "READ")}
        assert [a["level"] for a in fake.acl[("AtlasAgent", ids[1])]] == ["OWNER"]
        again = await backfill_agent_identity(uc)
        assert again["digital_created"] == 0 and again["registered"] == 0
    finally:
        await uc.aclose()
    async with get_sessionmaker()() as s:
        rows = list(await s.scalars(select(Agent).where(Agent.id.in_([uuid.UUID(i) for i in ids]))))
        assert all(a.digital_employee_uid is not None and a.auth_subject == "user" for a in rows)
