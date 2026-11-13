"""本机 CLI · 设备登记与建会话（agent-platform/doc/design/device-cli-design.html §2）。

配对码换设备令牌、核对令牌、默认设备、吊销；本机 CLI 智能体的保存规则；建会话时定下设备和目录。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from atlas_agent.main import create_app
from httpx import ASGITransport

from tests.fake_uc import AS_USER, TEST_USER

OTHER_USER = {"X-Uid": "00000000-0000-0000-0000-0000000000b2"}
REPORT = {"hostname": "MacBook-Pro", "os": "Darwin 24.1", "connector_version": "1.0.0"}


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


async def pair(client: httpx.AsyncClient, report: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await client.post("/v1/devices/pairing")
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    r = await client.post(
        "/internal/devices/redeem", json={"code": code, "report": report or REPORT}
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def device_agent(slug: str, **cli: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": "本机 Claude Code",
        "spec": {
            "kind": "cli",
            "model": {"model": "claude-opus-5"},
            "cli": {
                "cli_type": "claude-code",
                "placement": "device",
                "default_project_dir": "~/atlas-projects",
                **cli,
            },
        },
    }


# ═══════════════════════════════════ 设备登记 ═══════════════════════════════════


async def test_pairing_code_is_one_shot_and_token_authenticates(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    r = await client.post("/v1/devices/pairing")
    code = r.json()["code"]
    assert len(code) == 9 and code[4] == "-"
    # 大小写、短横线都不计较
    loose = code.replace("-", "").lower()
    issued = (
        await client.post("/internal/devices/redeem", json={"code": loose, "report": REPORT})
    ).json()
    assert issued["owner_uuid"] == TEST_USER and issued["token"].startswith("dvt_")
    again = await client.post("/internal/devices/redeem", json={"code": code})
    assert again.status_code == 400  # 只能用一次

    ok = await client.post(
        "/internal/devices/authenticate", json={"token": issued["token"], "report": REPORT}
    )
    assert ok.status_code == 200 and ok.json()["device_uuid"] == issued["device_uuid"]
    bad = await client.post("/internal/devices/authenticate", json={"token": "dvt_nope_nope"})
    assert bad.status_code == 401

    devices = (await client.get("/v1/devices")).json()["data"]
    assert len(devices) == 1
    d = devices[0]
    # 第一台自动成为默认；名字取电脑名；没部署设备网关时都算不在线
    assert d["is_default"] and d["name"] == "MacBook-Pro" and d["status"] == "offline"
    assert d["os"] == "Darwin 24.1" and d["connector_version"] == "1.0.0"
    assert d["last_seen_at"] is not None


async def test_default_moves_to_the_rest_when_revoked(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    first = await pair(client)
    second = await pair(client, {**REPORT, "hostname": "Office-PC"})
    r = await client.post(f"/v1/devices/{second['device_uuid']}/default")
    assert r.status_code == 200 and r.json()["is_default"]
    listed = (await client.get("/v1/devices")).json()["data"]
    assert [d["name"] for d in listed] == ["Office-PC", "MacBook-Pro"]  # 默认设备在前

    assert (await client.delete(f"/v1/devices/{second['device_uuid']}")).status_code == 204
    listed = (await client.get("/v1/devices")).json()["data"]
    assert [(d["name"], d["is_default"]) for d in listed] == [("MacBook-Pro", True)]
    # 吊销立即生效：令牌不再能连
    r = await client.post("/internal/devices/authenticate", json={"token": second["token"]})
    assert r.status_code == 401
    r = await client.post("/internal/devices/authenticate", json={"token": first["token"]})
    assert r.status_code == 200


async def test_only_the_owner_sees_and_manages_a_device(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    issued = await pair(client)
    device = issued["device_uuid"]
    r = await client.patch(f"/v1/devices/{device}", json={"name": "x"}, headers=OTHER_USER)
    assert r.status_code == 404
    assert (await client.delete(f"/v1/devices/{device}", headers=OTHER_USER)).status_code == 404
    assert (await client.get("/v1/devices", headers=OTHER_USER)).json()["data"] == []
    r = await client.patch(f"/v1/devices/{device}", json={"name": "我的 Mac"})
    assert r.json()["name"] == "我的 Mac"


# ═══════════════════════════════════ 本机 CLI 智能体 ═══════════════════════════════════


async def test_device_agent_save_rules(client: httpx.AsyncClient, clean_db: None) -> None:
    r = await client.post("/v1/agents", json=device_agent("dev-img", image="some/image"))
    assert r.status_code == 400 and "不用镜像" in r.text
    r = await client.post("/v1/agents", json=device_agent("dev-nodir", default_project_dir=""))
    assert r.status_code == 400 and "默认项目地址" in r.text
    r = await client.post("/v1/agents", json={**device_agent("dev-agent"), "auth_subject": "agent"})
    assert r.status_code == 400 and "以人为权限" in r.text

    r = await client.post("/v1/agents", json=device_agent("dev-ok", model_source="local_login"))
    assert r.status_code == 201, r.text
    cli = r.json()["spec"]["cli"]
    # 本机 CLI 不补镜像、适配器（会话 Pod 的部署默认值）
    assert cli["image"] == "" and cli["adapter"] == "" and cli["placement"] == "device"

    pod_spec = {**r.json()["spec"], "cli": {**cli, "placement": "pod", "model_source": "platform"}}
    r = await client.patch(f"/v1/agents/{r.json()['id']}", json={"spec": pod_spec})
    assert r.status_code == 400 and "运行位置" in r.text


async def test_thread_is_placed_on_the_default_device(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    agent = (await client.post("/v1/agents", json=device_agent("dev-thread"))).json()
    await client.post(f"/v1/agents/{agent['id']}/status", json={"status": "enabled"})
    r = await client.post("/v1/threads", json={"agent_id": agent["id"]})
    assert r.status_code == 400 and "还没有配对电脑" in r.text

    issued = await pair(client)
    thread = (await client.post("/v1/threads", json={"agent_id": agent["id"]})).json()
    assert thread["device"] == {
        "id": issued["device_uuid"],
        "name": "MacBook-Pro",
        "project_dir": f"~/atlas-projects/{thread['id']}",
        "status": "unknown",
    }

    chosen = await client.post(
        "/v1/threads",
        json={
            "agent_id": agent["id"],
            "device_id": issued["device_uuid"],
            "project_dir": "~/code/shop/",
        },
    )
    assert chosen.json()["device"]["project_dir"] == "~/code/shop"
    bad = await client.post(
        "/v1/threads", json={"agent_id": agent["id"], "project_dir": "~/code/../etc"}
    )
    assert bad.status_code == 400
    listed = (await client.get("/v1/threads")).json()["data"]
    assert {t["device"]["name"] for t in listed} == {"MacBook-Pro"}
