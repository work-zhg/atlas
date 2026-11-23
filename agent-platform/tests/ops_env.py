"""运营接口的测试工具：给应用配上运营密钥，按运营中心的方式签名调用（c4-model/agent/code-ops.html）。

def test_x(ops_key: str) -> None:        # 夹具：配上密钥（create_app 之前）
    r = await ops_call(client, "PUT", "/ops/v1/quotas/...", {"sandboxes": 2})
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_opsapi import OperatorStamp, sign

OPS_KEY = "test-ops-key"
OPERATOR = UUID("00000000-0000-0000-0000-0000000000f1")


@pytest.fixture
def ops_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("OPS_API_KEY", OPS_KEY)
    get_settings.cache_clear()
    yield OPS_KEY
    get_settings.cache_clear()


async def ops_call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: Any = None,
    *,
    idempotency_key: UUID | None = None,
    key: str = OPS_KEY,
) -> httpx.Response:
    """签好名的运营请求。写请求不给幂等键就每次新生成一个（不同的请求）。"""
    raw = b"" if body is None else json.dumps(body).encode()
    stamp = OperatorStamp(
        operator_uuid=OPERATOR,
        operator_name="值班",
        idempotency_key=idempotency_key or (uuid4() if method != "GET" else None),
    )
    headers = sign(key.encode(), method, path, raw, stamp=stamp)
    if body is not None:
        headers["Content-Type"] = "application/json"
    return await client.request(method, path, content=raw, headers=headers)
