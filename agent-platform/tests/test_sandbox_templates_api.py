"""沙箱模板的管理接口（改造计划 B2）。

只有 sandbox:manage 能用；提交前校验；修订不生效、发布才生效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from atlas_agent.db.models import SandboxTemplate
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import delete

from tests.fake_uc import FakeUC

GOOD = """spec:
  template:
    spec:
      securityContext:
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: sandbox
"""


class FakePublisher:
    def __init__(self) -> None:
        self.live: dict[str, str] = {}

    async def publish(self, template: SandboxTemplate) -> None:
        self.live[template.server] = template.sha()

    async def live_sha(self, server: str) -> str | None:
        return self.live.get(server)


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def users(fake_user_center: FakeUC) -> dict[str, dict[str, str]]:
    return {
        "member": {"X-Uid": fake_user_center.add("member", ["chat:use"])},
        "admin": {"X-Uid": fake_user_center.add("admin", ["chat:use", "sandbox:manage"])},
    }


@pytest.fixture
async def code() -> AsyncIterator[str]:
    value = f"t-{uuid4().hex[:8]}"
    yield value
    async with get_sessionmaker()() as session:
        await session.execute(delete(SandboxTemplate).where(SandboxTemplate.code == value))
        await session.commit()


async def test_only_sandbox_admins_see_templates(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    assert (await client.get("/v1/sandbox-templates", headers=users["member"])).status_code == 403
    r = await client.get("/v1/sandbox-templates", headers=users["admin"])
    assert r.status_code == 200, r.text
    seeded = {t["code"]: t for t in r.json()}
    # 迁移 0035 的种子：已发布的第 1 版
    assert {"sbx-strict", "cli", "bench"} <= set(seeded)
    assert seeded["sbx-strict"]["server"] == "sbx"
    assert seeded["sbx-strict"]["published_version"] is not None
    assert "yaml" not in seeded["sbx-strict"]


async def test_create_revise_publish(
    app: FastAPI, client: httpx.AsyncClient, users: dict[str, dict[str, str]], code: str
) -> None:
    admin = users["admin"]
    body = {"code": code, "description": "测试", "server": f"srv-{code}", "yaml": GOOD}
    bad = body | {"yaml": GOOD + "      hostNetwork: true\n"}
    r = await client.post("/v1/sandbox-templates", json=bad, headers=admin)
    assert r.status_code == 422 and "宿主命名空间" in r.json()["error"]["message"]

    r = await client.post("/v1/sandbox-templates", json=body, headers=admin)
    assert r.status_code == 201, r.text
    assert (r.json()["version"], r.json()["published_version"]) == (1, None)
    assert (await client.post("/v1/sandbox-templates", json=body, headers=admin)).status_code == 409

    revised = {"yaml": GOOD + "      enableServiceLinks: false\n"}
    r = await client.put(f"/v1/sandbox-templates/{code}", json=revised, headers=admin)
    assert r.status_code == 200 and r.json()["version"] == 2

    # 不在集群里：发布报依赖不可用，模板照样能查、能改
    r = await client.post(f"/v1/sandbox-templates/{code}/publish", headers=admin)
    assert r.status_code == 503, r.text

    app.state.template_publisher = FakePublisher()
    r = await client.post(f"/v1/sandbox-templates/{code}/publish", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["published_version"] == 2 and r.json()["drifted"] is False
    detail = (await client.get(f"/v1/sandbox-templates/{code}", headers=admin)).json()
    assert detail["yaml"] == revised["yaml"] and detail["drifted"] is False


async def test_unknown_template_is_404(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    r = await client.get("/v1/sandbox-templates/no-such", headers=users["admin"])
    assert r.status_code == 404
    r = await client.get(f"/v1/sandboxes/{uuid4()}/events", headers=users["admin"])
    assert r.status_code == 200 and r.json() == []
