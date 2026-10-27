"""持久指令（doc/context-management-design.html §10）：个人 / 平台两层，运行时读取。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from atlas_config.api.app import create_app


@pytest.fixture
async def client(storage) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(storage=storage))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://cfg", headers={"X-Uid": "tester"}
    ) as c:
        yield c

ALICE = {"X-Uid": "alice"}
BOB = {"X-Uid": "bob"}


async def test_personal_instructions_are_per_user(client: httpx.AsyncClient) -> None:
    r = await client.get("/config/instructions/me", headers=ALICE)
    assert r.status_code == 200 and r.json()["content"] == ""
    r = await client.put("/config/instructions/me", json={"content": "默认中文"}, headers=ALICE)
    assert r.status_code == 200 and r.json()["content"] == "默认中文"
    assert (await client.get("/config/instructions/me", headers=BOB)).json()["content"] == ""
    r = await client.put("/config/instructions/me", json={"content": "  改了  "}, headers=ALICE)
    assert r.json()["content"] == "改了"


async def test_platform_and_runtime_view(client: httpx.AsyncClient) -> None:
    await client.put("/config/instructions/platform", json={"content": "遵守合规"}, headers=ALICE)
    await client.put("/config/instructions/me", json={"content": "简洁回答"}, headers=BOB)
    got: Any = (await client.get("/internal/instructions", params={"user_id": "bob"})).json()
    assert got == {"platform": "遵守合规", "user": "简洁回答"}
    got = (await client.get("/internal/instructions", params={"user_id": "nobody"})).json()
    assert got == {"platform": "遵守合规", "user": ""}


async def test_length_is_capped(client: httpx.AsyncClient) -> None:
    r = await client.put("/config/instructions/me", json={"content": "x" * 10_001}, headers=ALICE)
    assert r.status_code == 422
