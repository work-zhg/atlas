"""运行期配置接进 Atlas（支线 C；c4-model/infra-platform/code-appconfig.html）。

多租户以后分两份管理接口（c4-model/agent/code-tenant-settings.html、code-ops.html）：
  · 租户设置（每人上限、默认智能体）在 /v1/tenant-settings/configs：看要登录，改要
    settings:manage，只看得到、改得到本租户的值；没改过的用系统值；
  · 系统参数（镜像白名单、上传上限、默认份额……）和租户设置的系统值在 /ops/v1/configs，只给
    运营中心。
经接口改了每人配额 / 镜像白名单，下一次分配沙箱就按新的判（读不缓存、不用重启）。库本身的行为
（类型、CAS、回滚）见 infra-platform/libs/appconfig/tests。
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
from tests.ops_env import ops_call
from tests.sandbox_env import component, new_workspace

QUOTA = "capacity.sandboxes_per_person"
IMAGES = "sandbox.image_allow_prefixes"
TENANT_KEYS = {
    QUOTA,
    "capacity.storage_mb_per_person",
    "capacity.runs_per_person",
    "tenant.default_agent",
}
OTHER_TENANT = "00000000-0000-0000-0000-00000000000b"


async def _reset(app: FastAPI) -> None:
    """测试库是共用的：Atlas 自己的 key 改过就删掉（各租户的行一起），下一次登记从默认值重来。"""
    names = list(app.state.runtime_config.config.keys)
    async with get_sessionmaker()() as session:
        ids = sa.select(config_table.c.config_uuid).where(config_table.c.key.in_(names))
        await session.execute(
            sa.delete(revision_table).where(revision_table.c.config_uuid.in_(ids))
        )
        await session.execute(sa.delete(config_table).where(config_table.c.key.in_(names)))
        await session.commit()


@pytest.fixture
async def app(clean_db: None, ops_key: str) -> AsyncIterator[FastAPI]:
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
        "admin": {"X-Uid": fake_user_center.add("admin", ["chat:use", "settings:manage"])},
        "outsider": {"X-Uid": fake_user_center.add("outsider", ["chat:use"], tenant=OTHER_TENANT)},
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
    """租户管理员改本租户的设置。"""
    path = f"/v1/tenant-settings/configs/{key}"
    current = (await client.get(path, headers=admin)).json()
    r = await client.put(
        path, json={"value": value, "expected_version": current["version"]}, headers=admin
    )
    assert r.status_code == 200, r.text


async def _set_system(client: httpx.AsyncClient, key: str, value: Any) -> None:
    """运营中心改系统参数。"""
    path = f"/ops/v1/configs/{key}"
    current = (await ops_call(client, "GET", path)).json()
    r = await ops_call(
        client, "PUT", path, {"value": value, "expected_version": current["version"]}
    )
    assert r.status_code == 200, r.text


async def test_tenant_admins_change_their_own_tenant_settings(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    base = "/v1/tenant-settings/configs"
    assert (await client.get(base)).status_code == 401
    r = await client.get(base, headers=users["member"])
    assert r.status_code == 200, r.text
    listed = {c["key"]: c for c in r.json()}
    assert set(listed) == TENANT_KEYS, "只列租户设置"
    # 没改过的取系统值，系统值没改过的取部署配置
    assert listed[QUOTA]["effective"] == get_settings().sandbox_quota_per_user
    assert listed[QUOTA]["overridden"] is False

    put = {"value": 1, "expected_version": listed[QUOTA]["version"]}
    member = await client.put(f"{base}/{QUOTA}", json=put, headers=users["member"])
    assert member.status_code == 403
    ok = await client.put(f"{base}/{QUOTA}", json=put, headers=users["admin"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["effective"] == 1
    stale = await client.put(f"{base}/{QUOTA}", json=put, headers=users["admin"])
    assert stale.status_code == 409
    [latest, *_] = (await client.get(f"{base}/{QUOTA}/revisions", headers=users["admin"])).json()
    assert latest["changed_by"] == users["admin"]["X-Uid"]

    # 别的租户不受影响；系统参数经这里改不了
    theirs = (await client.get(f"{base}/{QUOTA}", headers=users["outsider"])).json()
    assert theirs["effective"] == get_settings().sandbox_quota_per_user
    r = await client.get(f"{base}/{IMAGES}", headers=users["admin"])
    assert r.status_code == 422, r.text


async def test_system_params_are_for_the_ops_center_only(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    assert (await client.get("/v1/configs", headers=users["admin"])).status_code == 404
    assert (await client.get("/ops/v1/configs", headers=users["admin"])).status_code == 401
    r = await ops_call(client, "GET", "/ops/v1/configs")
    assert r.status_code == 200, r.text
    listed = {c["key"]: c for c in r.json()}
    assert {IMAGES, "capacity.default_running_runs", *TENANT_KEYS} <= set(listed)
    assert listed[QUOTA]["scope"] == "tenant" and listed[IMAGES]["scope"] == "system"

    # 运营中心改的是租户设置的系统值：没单独改过的租户都跟着变
    await _set_system(client, QUOTA, 3)
    mine = (
        await client.get(f"/v1/tenant-settings/configs/{QUOTA}", headers=users["member"])
    ).json()
    assert (mine["effective"], mine["inherited"]) == (3, 3)
    [latest, *_] = (await ops_call(client, "GET", f"/ops/v1/configs/{QUOTA}/revisions")).json()
    assert latest["changed_by"] == "运营：值班"


async def test_a_changed_quota_applies_to_the_next_allocation(
    client: httpx.AsyncClient,
    users: dict[str, dict[str, str]],
    fake: FakeOpenSandbox,
    redis: Any,
) -> None:
    c = component(fake, redis, sandbox_quota_per_user=10, sandbox_image_allow_prefixes=["other/"])
    ws = new_workspace()
    await c.native.ensure(ws, SandboxSpec())
    await _set(client, users["admin"], QUOTA, 1)
    # 同一个人换了镜像：要另建一个沙箱（同镜像的会共用，不占新配额）
    same_person = type(ws)(new_workspace().root_thread_uuid, ws.user_uuid)
    other_image = SandboxSpec(image="other/py:3.12")
    with pytest.raises(QuotaExceeded, match="上限 1"):
        await c.native.ensure(same_person, other_image)
    await _set(client, users["admin"], QUOTA, None)  # 恢复系统值（部署配置的 10）
    await c.native.ensure(same_person, other_image)


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
    await _set_system(client, IMAGES, ["registry.local/sandbox/"])
    await c.native.ensure(new_workspace(), image)
    assert fake.created[-1].spec["image"] == {"uri": "registry.local/sandbox/py:3.12"}


async def test_tenant_settings_page_shows_limits_quota_and_usage(
    client: httpx.AsyncClient, users: dict[str, dict[str, str]]
) -> None:
    await _set(client, users["admin"], QUOTA, 2)
    r = await client.get("/v1/tenant-settings", headers=users["member"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["per_person"]["sandboxes"] == 2 and body["can_manage"] is False
    assert body["quota"] == {
        "running_runs": None,
        "sandboxes": None,
        "storage_bytes": None,
        "is_default": True,
    }
    assert body["usage"] == {"running_runs": 0, "sandboxes": 0, "storage_bytes": 0}
    r = await client.get("/v1/tenant-settings", headers=users["admin"])
    assert r.json()["can_manage"] is True


async def test_browser_skill_not_installed_in_the_tenant_is_left_out() -> None:
    """系统参数 browser.skill 指的是技能市场里的版本：当前租户没装就不带（记日志），
    不让整轮失败。"""
    from uuid import UUID

    from atlas_agent.runtime_config import RuntimeConfig
    from atlas_agent.skills import FakeSkillDirectory, override_skill_directory
    from atlas_tenancy import TenantScope

    from tests.fake_uc import TEST_TENANT

    settings = get_settings().model_copy(
        update={"browser_skill": "browser@3", "browser_computer_use_skill": "computer-use@1"}
    )
    fake = FakeSkillDirectory()
    fake.add("browser", 3)
    override_skill_directory(fake)
    try:
        with TenantScope.enter(UUID(TEST_TENANT)):
            config = await RuntimeConfig(settings, get_sessionmaker()).browser_config()
    finally:
        override_skill_directory(None)
    assert config.skill is not None and (config.skill.slug, config.skill.version) == ("browser", 3)
    assert config.computer_use_skill is None
