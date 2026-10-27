"""切到 MCP 网关的迁移：登记原来经 MCP_SERVERS 接入的服务、批量发 Key。

用户中心里 Atlas 名下授权的搬迁在 Atlas 的 register-uc
（agent-platform/tests/test_register_mcp_retire.py）。
"""

from __future__ import annotations

import json

from atlas_mcpgateway.db.session import get_sessionmaker
from atlas_mcpgateway.migrate import (
    import_services,
    issue_missing,
    legacy_from_env,
    upstream_of,
)
from atlas_mcpgateway.registry import Registry
from atlas_mcpgateway.settings import get_settings
from fake_higress import FakeHigress
from gateway_testkit import ADMIN, ALICE, FakePermissions, weather_and_ticket


def test_upstream_name_comes_from_the_higress_path() -> None:
    assert upstream_of("http://192.168.0.104:18080/mcp-servers/serpapi") == "serpapi"
    assert upstream_of("http://higress.atlas-infra:8080/mcp-servers/serpapi/sse") == "serpapi"
    assert upstream_of("https://example.com/mcp") is None


async def test_import_from_env_registers_higress_services(
    perms: FakePermissions, higress: FakeHigress
) -> None:
    weather_and_ticket(higress)
    servers = legacy_from_env(
        json.loads(
            json.dumps(
                [
                    {
                        "name": "weather",
                        "url": "http://h:18080/mcp-servers/weather",
                        "call_timeout_s": 90,
                    },
                    {"name": "report", "url": "http://h:18080/mcp-servers/report"},
                    {"name": "direct", "url": "https://example.com/mcp"},
                ]
            )
        )
    )
    async with get_sessionmaker()() as session:
        report = await import_services(session, get_settings(), perms, servers, owner=ADMIN)
        await session.commit()
    assert report.imported == ["weather"]
    skipped = dict(report.skipped)
    assert "接入检查不通过" in skipped["report"]  # Higress 上没开认证
    assert "不是 Higress 上的服务" in skipped["direct"]
    assert perms.grants["weather"] == {ADMIN: "OWNER"}

    async with get_sessionmaker()() as session:
        registry = Registry(session, get_settings())
        weather = await registry.get("weather")
        assert weather.call_timeout_s == 90
        assert weather.review_required is False  # env 方式的服务默认不要求复核
        assert [v.status for v in await registry.tools(weather)] == ["ok"]
        again = await import_services(session, get_settings(), perms, servers[:1], owner=ADMIN)
    assert again.existing == ["weather"]


async def test_issue_missing_keys_for_holders(perms: FakePermissions, higress: FakeHigress) -> None:
    weather_and_ticket(higress)
    servers = legacy_from_env([{"name": "weather", "url": "http://h:18080/mcp-servers/weather"}])
    async with get_sessionmaker()() as session:
        await import_services(session, get_settings(), perms, servers, owner=ADMIN)
        perms.allow("weather", ALICE)
        issued = await issue_missing(session, perms, ["weather"], actor=ADMIN)
        again = await issue_missing(session, perms, ["weather"], actor=ADMIN)
        await session.commit()
    assert sorted(uid for uid, _ in issued) == sorted([ADMIN, ALICE])
    assert again == []
