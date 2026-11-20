"""知识库文件计入租户的存储份额（多租户设计 §9；运营接口的容量契约）。

份额   运营中心单独给租户定；没定过的用默认份额（系统参数）
判断   上传前：已用 + 这次的 > 份额 → 拒绝（说明是租户份额满了），什么都不存
用量   只有数字：租户管理员看本租户的，运营中心看各租户的
"""

from __future__ import annotations

import json
from typing import Any

from atlas_appconfig.db import config_table, revision_table
from atlas_knowledge.capacity import DEFAULT_STORAGE_MB
from atlas_knowledge.db.session import get_sessionmaker
from conftest import OTHER_TENANT, TEST_TENANT, ops_request
from sqlalchemy import delete, select

ALICE = {"X-Uid": "alice"}
BOB_ELSEWHERE = {"X-Uid": "bob", "X-Tenant-Id": OTHER_TENANT}
CUSTOM = {
    "mode": "custom",
    "rules": {"segmentation": {"separator": "\\n\\n", "max_tokens": 200, "chunk_overlap": 0}},
}


async def _ops(client: Any, method: str, path: str, body: object = None) -> Any:
    return await client.request(method, path, **ops_request(method, path, body))


async def _dataset(client: Any, headers: dict[str, str] = ALICE) -> str:
    r = await client.post(
        "/v1/datasets",
        json={"name": "制度库", "embedding_model": "fake-embedding"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]  # type: ignore[no-any-return]


async def _upload(client: Any, ds: str, size: int, headers: dict[str, str] = ALICE) -> Any:
    return await client.post(
        f"/v1/datasets/{ds}/documents",
        files=[("files", ("a.txt", b"x" * size, "text/plain"))],
        data={"process_rule": json.dumps(CUSTOM)},
        headers=headers,
    )


async def _reset_default() -> None:
    async with get_sessionmaker()() as s:
        ids = select(config_table.c.config_uuid).where(
            config_table.c.key == DEFAULT_STORAGE_MB.name
        )
        await s.execute(delete(revision_table).where(revision_table.c.config_uuid.in_(ids)))
        await s.execute(delete(config_table).where(config_table.c.key == DEFAULT_STORAGE_MB.name))
        await s.commit()


async def test_upload_is_refused_when_the_tenant_quota_is_full(client: Any) -> None:
    ds = await _dataset(client)
    path = f"/ops/v1/quotas/{TEST_TENANT}"
    r = await _ops(client, "PUT", path, {"storage_bytes": 100})
    assert r.status_code == 200, r.text
    assert r.json()["quota"] == {"storage_bytes": 100} and r.json()["is_default"] is False

    assert (await _upload(client, ds, 60)).status_code == 201
    r = await _upload(client, ds, 50)
    assert r.status_code == 409 and "租户份额" in r.json()["detail"], r.text
    # 租户管理员看得到本租户的份额和用量；别的租户不受影响
    mine = (await client.get("/v1/capacity", headers=ALICE)).json()
    assert mine == {"quota_bytes": 100, "used_bytes": 60, "is_default": False}
    other_ds = await _dataset(client, BOB_ELSEWHERE)
    assert (await _upload(client, other_ds, 500, BOB_ELSEWHERE)).status_code == 201

    usage = (await _ops(client, "GET", f"/ops/v1/usage/{TEST_TENANT}")).json()
    assert usage["usage"] == {"storage_bytes": 60}
    total = (await _ops(client, "GET", "/ops/v1/capacity")).json()
    assert total["usage"]["storage_bytes"] == 560
    # 回到默认份额（默认 0 = 不限）：又能传了
    r = await _ops(client, "PUT", path, {"use_default": True})
    assert r.json()["is_default"] is True and r.json()["quota"] == {"storage_bytes": None}
    assert (await _upload(client, ds, 50)).status_code == 201


async def test_default_quota_is_a_system_param(client: Any) -> None:
    await client._transport.app.state.capacity.config.register()  # type: ignore[attr-defined]
    try:
        r = await _ops(client, "PUT", "/ops/v1/quotas/default", {"storage_bytes": 3 * 1024 * 1024})
        assert r.status_code == 200 and r.json() == {"storage_bytes": 3 * 1024 * 1024}, r.text
        quotas = (await _ops(client, "GET", "/ops/v1/quotas")).json()
        assert quotas["default"] == {"storage_bytes": 3 * 1024 * 1024}
        # 系统参数接口里也看得到（有修订历史）
        configs = (await _ops(client, "GET", "/ops/v1/configs")).json()
        assert any(c["key"] == DEFAULT_STORAGE_MB.name for c in configs)
        mine = (await client.get("/v1/capacity", headers=ALICE)).json()
        assert mine["quota_bytes"] == 3 * 1024 * 1024 and mine["is_default"] is True
    finally:
        await _reset_default()
