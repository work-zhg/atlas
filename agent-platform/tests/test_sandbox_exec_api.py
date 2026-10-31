"""接线：智能体的 sandbox 配置进出版本快照；POST /v1/threads/{id}/sandbox/exec 不新建沙箱。

执行本身（只认评测档、先写文件再执行）见 test_sandbox_bench_profile.py。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_agent.main import create_app
from httpx import ASGITransport

from tests.fake_uc import AS_USER

MODEL = {"model": "claude-opus-5"}


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AS_USER) as c:
        yield c


@pytest.mark.usefixtures("clean_db")
async def test_sandbox_config_round_trips_and_exec_never_creates_a_sandbox(
    client: httpx.AsyncClient,
) -> None:
    sandbox = {"image": "tb/regex-log:1", "profile": "bench"}
    agent = await client.post(
        "/v1/agents",
        json={"slug": "bench-host", "name": "评测", "spec": {"model": MODEL, "sandbox": sandbox}},
    )
    assert agent.status_code == 201, agent.text
    assert agent.json()["spec"]["sandbox"] == sandbox

    thread = await client.post("/v1/threads", json={"agent_id": agent.json()["id"]})
    res = await client.post(
        f"/v1/threads/{thread.json()['id']}/sandbox/exec", json={"command": "true"}
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"]["kind"] == "sandbox_not_running"


@pytest.mark.usefixtures("clean_db")
async def test_acp_agent_rejects_a_sandbox_config(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/v1/agents",
        json={
            "slug": "acp-sbx",
            "name": "acp",
            "spec": {
                "model": MODEL,
                "kind": "acp",
                "cli": {"cli_type": "claude-code", "image": "img"},
                "sandbox": {"image": "x"},
            },
        },
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["kind"] == "invalid_spec"
