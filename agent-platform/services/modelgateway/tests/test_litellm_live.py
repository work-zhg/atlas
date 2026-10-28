"""对着真实的 LiteLLM 跑一遍平台的主流程（按需运行：没配 LiteLLM 就跳过）。

    ATLAS_MODELGW_LIVE_LITELLM_URL=http://127.0.0.1:4000 \\
    ATLAS_MODELGW_LIVE_LITELLM_MASTER_KEY=sk-… \\
    uv run pytest agent-platform/services/modelgateway/tests/test_litellm_live.py

★ 替身（FakeLiteLLM）是按 LiteLLM 1.106 的实测行为写的；升级 LiteLLM 后跑一次这个，确认没变。
★ 只建名字带 live- 前缀的模型和 Key，结束时删掉；供应商凭据是假的，不会真的调到供应商。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from modelgw_testkit import ADMIN, ALICE, as_app, as_user

LIVE_URL = os.environ.get("ATLAS_MODELGW_LIVE_LITELLM_URL")
LIVE_KEY = os.environ.get("ATLAS_MODELGW_LIVE_LITELLM_MASTER_KEY")

pytestmark = pytest.mark.skipif(
    not (LIVE_URL and LIVE_KEY), reason="没配真实的 LiteLLM（ATLAS_MODELGW_LIVE_LITELLM_*）"
)
H = as_user(ADMIN)


@pytest.fixture
async def live(perms: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    from atlas_modelgateway.api.app import create_app
    from atlas_modelgateway.settings import get_settings

    monkeypatch.setenv("ATLAS_MODELGW_LITELLM_BASE_URL", str(LIVE_URL))
    monkeypatch.setenv("ATLAS_MODELGW_LITELLM_MASTER_KEY", str(LIVE_KEY))
    get_settings.cache_clear()
    app = create_app(perms=perms)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://platform"
            ) as client,
        ):
            yield client
            await _cleanup(client)
    finally:
        get_settings.cache_clear()


async def _cleanup(client: httpx.AsyncClient) -> None:
    for key in (await client.get("/api/v1/client-keys", headers=H)).json():
        if key["client_code"].startswith("live-"):
            await client.delete(f"/api/v1/client-keys/{key['uuid']}", headers=H)
    await client.delete("/api/v1/keys/me", headers=as_user(ALICE))
    for m in (await client.get("/api/v1/models", headers=H)).json()["data"]:
        if m["name"].startswith("live-") and m["uuid"]:
            await client.delete(f"/api/v1/models/{m['uuid']}", headers=H)


async def _litellm_key(secret: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=str(LIVE_URL), trust_env=False) as h:
        r = await h.get(
            "/key/info", params={"key": secret}, headers={"Authorization": f"Bearer {LIVE_KEY}"}
        )
        return dict(r.json()["info"])


async def test_full_flow_against_real_litellm(live: httpx.AsyncClient) -> None:
    created = await live.post(
        "/api/v1/models",
        json={
            "name": "live-chat",
            "display_name": "Live Chat",
            "provider": "deepseek",
            "visibility": "public",
            "api_key": "sk-fake-provider",
            "max_input_tokens": 131072,
            "max_output_tokens": 8192,
            "input_price": 0.28,
            "output_price": 0.42,
        },
        headers=H,
    )
    assert created.status_code == 201, created.text
    model = created.json()
    assert model["status"] == "ok"
    assert model["max_input_tokens"] == 131072 and model["input_price"] == 0.28

    updated = await live.put(
        f"/api/v1/models/{model['uuid']}",
        json={
            "display_name": "Live Chat",
            "provider": "deepseek",
            "visibility": "public",
            "max_input_tokens": 65536,
            "input_price": 0.5,
        },
        headers=H,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["max_input_tokens"] == 65536 and updated.json()["input_price"] == 0.5

    test = await live.post(
        "/api/v1/models/test-connection",
        json={"name": "live-chat", "provider": "deepseek", "api_key": "sk-fake-provider"},
        headers=H,
    )
    assert test.json()["ok"] is False and "stack trace" not in test.json()["message"]

    applied = await live.post(
        "/api/v1/keys/me", json={"models": ["live-chat"]}, headers=as_user(ALICE)
    )
    assert applied.status_code == 201, applied.text
    secret = applied.json()["secret"]
    assert (await _litellm_key(secret))["models"] == ["live-chat"]

    await live.post(
        "/internal/client-keys",
        json={"client_type": "Agent", "client_code": "live-agent", "name": "Live"},
        headers=as_app(),
    )
    key = next(
        k
        for k in (await live.get("/api/v1/client-keys", headers=H)).json()
        if k["client_code"] == "live-agent"
    )
    assigned = await live.put(
        f"/api/v1/client-keys/{key['uuid']}/models", json={"models": ["live-chat"]}, headers=H
    )
    assert assigned.status_code == 200, assigned.text
    got = (
        await live.get(
            "/internal/client-keys",
            params={"client_type": "Agent", "client_code": "live-agent"},
            headers=as_app(),
        )
    ).json()
    assert got["status"] == "active" and got["models"][0]["max_input_tokens"] == 65536

    await live.post(f"/api/v1/client-keys/{key['uuid']}/block", headers=H)
    assert (await _litellm_key(got["key"]))["blocked"] is True
    reset = await live.post(f"/api/v1/client-keys/{key['uuid']}/reset", headers=H)
    assert reset.status_code == 200, reset.text
    new = (
        await live.get(
            "/internal/client-keys",
            params={"client_type": "Agent", "client_code": "live-agent"},
            headers=as_app(),
        )
    ).json()
    assert new["status"] == "blocked"

    # 删模型：Key 的有效模型为空 → LiteLLM 里停用，而不是推空列表
    deleted = await live.delete(f"/api/v1/models/{model['uuid']}", headers=H)
    assert deleted.status_code == 200, deleted.text
    assert (await _litellm_key(secret))["blocked"] is True
