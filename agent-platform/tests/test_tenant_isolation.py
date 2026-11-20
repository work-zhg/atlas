"""租户之间互不可见（c4-model/opscenter/design-tenant-isolation.html 原理 1、2）。

验收的就是当初发现的问题：「全部智能体 · 所有者」这类编码级授权到了 Atlas 曾经变成完全不过滤，
某个租户的管理员会看到所有租户的智能体。现在先按租户自动过滤，编码级授权只覆盖本租户。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from atlas_agent.auth.uc import UCClient
from atlas_agent.config import get_settings
from atlas_agent.main import create_app
from httpx import ASGITransport

from tests.fake_uc import FakeUC

SPEC = {"system_prompt": "p", "model": {"model": "claude-opus-5"}, "tool_names": []}
OTHER_TENANT = "00000000-0000-0000-0000-00000000000b"


class _NoopExecutor:
    async def dispatch(self, run_id: Any) -> None:
        return None


def _client(app: Any, uid: str, **headers: str) -> httpx.AsyncClient:
    h = {"X-Caller-App": "gateway", "X-Uid": uid, **headers}
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=h)


async def test_tenants_cannot_see_each_others_agents_and_threads(clean_db: None) -> None:
    fake = FakeUC()
    app = create_app()
    app.state.executor = _NoopExecutor()
    app.state.uc = UCClient(get_settings(), transport=httpx.MockTransport(fake.handler))
    ops = ["chat:use", "agent:create"]
    alice = fake.add("alice", ops)  # 测试租户
    bob = fake.add("bob", ops, tenant=OTHER_TENANT)
    # 两个人都是各自租户的智能体管理员：「全部智能体 · 所有者」
    fake.data_levels[alice] = {"AtlasAgent": "OWNER"}
    fake.data_levels[bob] = {"AtlasAgent": "OWNER"}
    slug = f"same-{uuid.uuid4().hex[:8]}"
    try:
        async with _client(app, alice) as a, _client(app, bob) as b:
            r = await a.post("/v1/agents", json={"slug": slug, "name": "通用助手", "spec": SPEC})
            assert r.status_code == 201, r.text
            agent = r.json()["id"]
            thread = (await a.post("/v1/threads", json={"agent_id": agent})).json()["id"]

            # 名字只在租户内唯一：另一个租户可以用同一个 slug
            r = await b.post("/v1/agents", json={"slug": slug, "name": "合同审查", "spec": SPEC})
            assert r.status_code == 201, r.text
            bobs = r.json()["id"]

            listed = {i["id"] for i in (await b.get("/v1/agents")).json()["data"]}
            assert bobs in listed and agent not in listed
            assert (await b.get(f"/v1/agents/{agent}")).status_code == 404
            assert (await b.get(f"/v1/threads/{thread}")).status_code == 404
            assert (await b.post("/v1/threads", json={"agent_id": agent})).status_code == 404

            # 请求头冒用别的租户：用户中心在那个租户里找不到他
            async with _client(app, bob, **{"X-Tenant-Id": str(fake.tenants[alice])}) as forged:
                assert (await forged.get("/v1/agents")).status_code == 401
    finally:
        await app.state.uc.aclose()
