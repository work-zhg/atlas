"""运行期配置接进 Atlas（支线 C；c4-model/infra-platform/code-appconfig.html）。

管理接口只给 config:manage，修订里记操作人；经接口改了每人配额 / 镜像白名单，下一次分配沙箱就按新的
判（读不缓存、不用重启）。库本身的行为（类型、CAS、回滚）见 infra-platform/libs/appconfig/tests。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from atlas_agent.config import get_settings
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.main import create_app
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.native import ImageDenied
from atlas_agent.sandbox.provisioning.model import QuotaExceeded
from atlas_appconfig.db import config_table, revision_table
from atlas_engine.contracts.spec import SandboxSpec
from fastapi import FastAPI
from httpx import ASGITransport

from tests.fake_uc import FakeUC
from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import component, new_workspace

QUOTA = "sandbox.quota_per_user"
IMAGES = "sandbox.image_allow_prefixes"


async def _reset(app: FastAPI) -> None:
    """测试库是共用的：Atlas 自己的 key 改过就删掉，下一次登记从默认值重来。"""
    names = list(app.state.runtime_config.config.keys)
    async with get_sessionmaker()() as session:
        ids = sa.select(config_table.c.config_uuid).where(config_table.c.key.in_(names))
        await session.execute(
            sa.delete(revision_table).where(revision_table.c.config_uuid.in_(ids))
        )
        await session.execute(sa.delete(config_table).where(config_table.c.key.in_(names)))
        await session.commit()


@pytest.fixture
async def app(clean_db: None) -> AsyncIterator[FastAPI]:
    app = create_app()
    await _reset(app)
    await app.state.runtime_config.config.register()  # lifespan 里做的那一步
    yield app
    await _reset(app)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def users(fake_user_center: FakeUC) -> dict[str, dict[str, str]]:
    return {
        "member": {"X-Uid": fake_user_center.add("member", ["chat:use"])},
        "admin": {"X-Uid": fake_user_center.add("admin", ["chat:use", "config:manage"])},
    }


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


async def _set(client: httpx.AsyncClient, admin: dict[str, str], key: str, value: Any) -> None:
    current = (await client.get(f"/v1/configs/{key}", headers=admin)).json()
    r = await client.put(
        f"/v1/configs/{key}",
        json={"value": value, "expected_version": current["version"]},
        headers=admin,
    )
    assert r.status_code == 200, r.text


async def test_only_config_admins_see_and_change_configs(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    assert (await client.get("/v1/configs", headers=users["member"])).status_code == 403
    assert (await client.get("/v1/configs")).status_code == 401
    r = await client.get("/v1/configs", headers=users["admin"])
    assert r.status_code == 200, r.text
    listed = {c["key"]: c for c in r.json()}
    assert set(listed) == {QUOTA, IMAGES, "sandbox.bench_image_allow_prefixes"}
    # 没改过的取部署配置
    assert listed[QUOTA]["effective"] == get_settings().sandbox_quota_per_user
    assert listed[QUOTA]["overridden"] is False

    put = {"value": 1, "expected_version": listed[QUOTA]["version"]}
    member = await client.put(f"/v1/configs/{QUOTA}", json=put, headers=users["member"])
    assert member.status_code == 403
    assert (
        await client.put(f"/v1/configs/{QUOTA}", json=put, headers=users["admin"])
    ).status_code == 200
    stale = await client.put(f"/v1/configs/{QUOTA}", json=put, headers=users["admin"])
    assert stale.status_code == 409
    [latest, *_] = (
        await client.get(f"/v1/configs/{QUOTA}/revisions", headers=users["admin"])
    ).json()
    assert latest["changed_by"] == users["admin"]["X-Uid"]


async def test_a_changed_quota_applies_to_the_next_allocation(
    client: httpx.AsyncClient,
    users: dict[str, dict[str, str]],
    fake: FakeOpenSandbox,
    redis: Any,
) -> None:
    c = component(fake, redis, sandbox_quota_per_user=10)
    ws = new_workspace()
    await c.native.ensure(ws, SandboxSpec())
    await _set(client, users["admin"], QUOTA, 1)
    same_person = type(ws)(new_workspace().root_thread_uuid, ws.user_uuid)
    with pytest.raises(QuotaExceeded, match="上限 1"):
        await c.native.ensure(same_person, SandboxSpec())
    await _set(client, users["admin"], QUOTA, None)  # 恢复默认（部署配置的 10）
    await c.native.ensure(same_person, SandboxSpec())


async def test_a_changed_image_allowlist_applies_to_the_next_sandbox(
    client: httpx.AsyncClient,
    users: dict[str, dict[str, str]],
    fake: FakeOpenSandbox,
    redis: Any,
) -> None:
    c = component(fake, redis, sandbox_image_allow_prefixes=[])
    image = SandboxSpec(image="registry.local/sandbox/py:3.12")
    with pytest.raises(ImageDenied):
        await c.native.ensure(new_workspace(), image)
    await _set(client, users["admin"], IMAGES, ["registry.local/sandbox/"])
    await c.native.ensure(new_workspace(), image)
    assert fake.created[-1].spec["image"] == {"uri": "registry.local/sandbox/py:3.12"}
