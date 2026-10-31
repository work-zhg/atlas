"""持久指令（doc/context-management-design.html §10）：个人 / 平台两层，运行时每轮读取。

★ 开发与测试共用一个库：个人指令用一次性的用户、测完删掉；平台指令全局一份，测完恢复原样。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from atlas_agent.db.models import Instruction
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.context import load_instructions
from atlas_agent.main import create_app
from httpx import ASGITransport
from sqlalchemy import delete

from tests.fake_uc import FakeUC


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def users(fake_user_center: FakeUC) -> AsyncIterator[dict[str, str]]:
    """一次性的用户：member 只能对话，admin 另有 model:manage。测完删掉他们的个人指令。"""
    made = {
        "member": fake_user_center.add("member", ["chat:use"]),
        "admin": fake_user_center.add("admin", ["chat:use", "model:manage"]),
    }
    yield made
    async with get_sessionmaker()() as s:
        await s.execute(delete(Instruction).where(Instruction.owner_id.in_(made.values())))
        await s.commit()


@pytest.fixture
async def keep_platform() -> AsyncIterator[None]:
    async with get_sessionmaker()() as s:
        row = await s.get(Instruction, ("platform", ""))
        saved = None if row is None else (row.content, row.updated_by, row.updated_at)
    yield
    async with get_sessionmaker()() as s:
        row = await s.get(Instruction, ("platform", ""))
        if saved is None and row is not None:
            await s.delete(row)
        elif saved is not None and row is not None:
            row.content, row.updated_by, row.updated_at = saved
        await s.commit()


def _as(uid: str) -> dict[str, str]:
    return {"X-Uid": uid}


async def test_personal_instructions_are_per_user(
    client: httpx.AsyncClient, users: dict[str, str]
) -> None:
    member, admin = _as(users["member"]), _as(users["admin"])
    r = await client.get("/v1/instructions/me", headers=member)
    assert r.status_code == 200 and r.json()["content"] == ""
    r = await client.put("/v1/instructions/me", json={"content": "默认中文"}, headers=member)
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "默认中文" and r.json()["updated_by"] == users["member"]
    assert (await client.get("/v1/instructions/me", headers=admin)).json()["content"] == ""
    r = await client.put("/v1/instructions/me", json={"content": "  改了  "}, headers=member)
    assert r.json()["content"] == "改了"
    # 每层有长度上限
    r = await client.put("/v1/instructions/me", json={"content": "x" * 10_001}, headers=member)
    assert r.status_code == 422


@pytest.mark.usefixtures("keep_platform")
async def test_platform_needs_model_manage_and_reaches_runtime(
    client: httpx.AsyncClient, users: dict[str, str]
) -> None:
    member, admin = _as(users["member"]), _as(users["admin"])
    r = await client.put("/v1/instructions/platform", json={"content": "越权"}, headers=member)
    assert r.status_code == 403
    r = await client.put("/v1/instructions/platform", json={"content": "遵守合规"}, headers=admin)
    assert r.status_code == 200, r.text
    # 平台指令人人可读
    assert (await client.get("/v1/instructions/platform", headers=member)).json()[
        "content"
    ] == "遵守合规"
    await client.put("/v1/instructions/me", json={"content": "简洁回答"}, headers=member)

    # 运行时：平台在前、个人在后（后面的优先）；别人的个人指令不会混进来
    text = await load_instructions(get_sessionmaker(), None, UUID(users["member"]))
    assert text.index("遵守合规") < text.index("简洁回答")
    text = await load_instructions(get_sessionmaker(), None, users["admin"])
    assert "遵守合规" in text and "简洁回答" not in text
