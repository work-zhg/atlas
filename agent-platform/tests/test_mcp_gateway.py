"""Atlas 经自研 MCP 网关调 MCP：按智能体的 Key 判权（MCP 网关原型）。

MCP 网关用真实现：atlas_mcpgateway 的 app 经 ASGITransport 跑在进程里（它自己的测试库
atlas_mcpgateway_test），用户中心换成内存替身；上游 MCP 服务是后台线程里的替身。
Atlas 的内部接口调用、MCP 客户端（FastMCP / httpx2）都经 ASGITransport 连到这个网关。

## 这组测试真正在守什么

  · 闸门：智能体的 Key 没被授权的服务、工具，管理员停用的工具，装配时跳过并提示；
    编辑器只列能用的；新建的智能体只能选公开服务
  · 身份：Key 在传输层，网关按 Key 写入身份（X-Auth-Uid = Key 的主体），模型改不了
  · 调用失败：参数不合法不发请求；Key 刚被重置自动重取重试一次；Key 停用、收回授权在本 Run 段内
    记住、不再发请求；有副作用的工具超时要说「结果未知」
  · ACP：下发网关地址和智能体 Key，没授权的服务不下发
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.providers.mcp.acp import acp_mcp_servers
from atlas_agent.providers.mcp.gateway import agent_access, gateway_catalog, reset_gateway_cache

# 夹具 gateway_environment / gateway / upstream 在 conftest.py（来自 tests/gateway_env.py）
from tests.gateway_env import (
    ADMIN,
    AGENT,
    ALICE,
    GATEWAY,
    MCP_TOKEN,
    FakeMcp,
    FakeMcpService,
    agent_key,
    as_user,
    assemble,
    call,
    grant,
    register,
    text_of,
)

pytestmark = pytest.mark.usefixtures("gateway_environment")


@pytest.fixture
async def env(gateway: Any, upstream: FakeMcp) -> Any:
    """weather（公开、只读）；ticket（私有：create_ticket、delete_ticket）。

    → (app, perms, c, key)。
    """
    weather = FakeMcpService()
    weather.tool(
        "get_weather",
        "查询某个城市今天的天气。",
        lambda args, h: (
            f"{args['city']}：小雨（主体={h.get('x-auth-uid')} 使用者={h.get('x-uid')}）"
        ),
        properties={"city": {"type": "string"}},
        annotations={"readOnlyHint": True},
    )
    ticket = FakeMcpService()

    async def create(args: dict[str, Any], h: dict[str, str]) -> str:
        if args.get("title") == "slow":
            await asyncio.sleep(1.5)
        return f"T-001 {args['title']} 主体={h.get('x-auth-uid')}"

    ticket.tools["create_ticket"] = {
        "name": "create_ticket",
        "description": "在工单系统里建一张工单。",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    }
    ticket.handlers["create_ticket"] = create
    ticket.tool("delete_ticket", "删除一张工单。", lambda _a, _h: "deleted")
    upstream.services.update({"weather": weather, "ticket": ticket})
    app, perms, c = gateway
    await register(c, "weather", visibility="public", upstream=upstream)
    await register(c, "ticket", upstream=upstream)
    key = await agent_key(c)
    return app, perms, c, key


# ───────────────────────────── 装配：闸门


async def test_public_service_works_and_private_needs_grant(env: Any) -> None:
    _app, _perms, c, key = env
    tools, notices, _ = await assemble(["mcp:weather:get_weather", "mcp:ticket:create_ticket"])
    assert [t.name for t in tools] == ["weather__get_weather"]  # 公开服务：不用授权
    reasons = {d["server"]: d["reason"] for k, d in notices if k is EventType.MCP_TOOL_SKIPPED}
    assert reasons == {"ticket": "forbidden"}

    await grant(c, "ticket", key, "all")
    tools, notices, _ = await assemble(["mcp:ticket:create_ticket"])
    assert [t.name for t in tools] == ["ticket__create_ticket"]
    assert notices == []


async def test_tool_level_grant_and_disabled_tool_are_skipped(env: Any) -> None:
    _app, _perms, c, key = env
    await grant(c, "ticket", key, "tools", ["create_ticket"])
    tools, notices, _ = await assemble(["mcp:ticket:create_ticket", "mcp:ticket:delete_ticket"])
    assert [t.name for t in tools] == ["ticket__create_ticket"]
    skipped = [d for k, d in notices if k is EventType.MCP_TOOL_SKIPPED]
    assert skipped[0]["tools"] == ["mcp:ticket:delete_ticket"]
    assert skipped[0]["reason"] == "forbidden"

    # 管理员停用：跳过并提示，不让整轮失败
    r = await c.post("/api/v1/services/weather/tools/get_weather/disable", headers=as_user(ADMIN))
    assert r.status_code == 200
    reset_gateway_cache()
    tools, notices, _ = await assemble(["mcp:weather:get_weather"])
    assert tools == []
    assert [d["reason"] for k, d in notices if k is EventType.MCP_TOOL_SKIPPED] == ["unavailable"]


# ───────────────────────────── 调用


async def test_call_carries_key_subject(env: Any, upstream: FakeMcp) -> None:
    _app, _perms, _c, key = env
    tools, _, sessions = await assemble(["mcp:weather:get_weather"])
    try:
        for city in ("上海", "北京"):
            result = await call(tools, "weather__get_weather", city=city)
            # X-Auth-Uid = 智能体 Key 的主体；X-Uid = Atlas 带来的使用者
            assert f"主体={key['subject_uid']} 使用者={ALICE}" in text_of(result)
    finally:
        await sessions.aclose()
    seen = upstream.services["weather"].calls()
    assert len(seen) == 2
    # 上游收到的是网关注入的上游凭据，不是智能体的 Key
    assert all(h["authorization"] == f"Bearer {MCP_TOKEN}" for h, _p in seen)


async def test_invalid_arguments_go_back_to_model_without_request(
    env: Any, upstream: FakeMcp
) -> None:
    _app, _perms, c, key = env
    await grant(c, "ticket", key, "all")
    tools, _, sessions = await assemble(["mcp:ticket:create_ticket"])
    try:
        result = await call(tools, "ticket__create_ticket", titel="拼错了")
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "参数不合法" in text_of(result)
    assert "titel" in text_of(result)
    assert upstream.services["ticket"].calls() == []


async def test_reset_key_is_refetched_and_retried_once(env: Any) -> None:
    _app, _perms, c, key = env
    tools, _, sessions = await assemble(["mcp:weather:get_weather"])
    try:
        assert "小雨" in text_of(await call(tools, "weather__get_weather", city="上海"))
        await c.post(f"/api/v1/keys/{key['id']}/reset", headers=as_user(ADMIN))
        result = await call(tools, "weather__get_weather", city="上海")
    finally:
        await sessions.aclose()
    assert result.status != "error", text_of(result)
    assert "小雨" in text_of(result)


async def test_blocked_key_is_reported_and_remembered(env: Any, upstream: FakeMcp) -> None:
    _app, _perms, c, key = env
    tools, _, sessions = await assemble(["mcp:weather:get_weather"])
    try:
        await call(tools, "weather__get_weather", city="上海")
        await c.post(f"/api/v1/keys/{key['id']}/block", headers=as_user(ADMIN))
        first = await call(tools, "weather__get_weather", city="上海")
        sent = len(upstream.services["weather"].seen)
        second = await call(tools, "weather__get_weather", city="北京")
    finally:
        await sessions.aclose()
    assert first.status == "error"
    assert "API Key 已失效或已停用" in text_of(first)
    assert "不要重试" in text_of(first)
    # 本 Run 段内记住：第二次不再发请求
    assert text_of(second) == text_of(first)
    assert len(upstream.services["weather"].seen) == sent


async def test_revoked_grant_mid_run_is_remembered_per_service(env: Any, upstream: FakeMcp) -> None:
    _app, _perms, c, key = env
    await grant(c, "ticket", key, "all")
    tools, _, sessions = await assemble(["mcp:ticket:create_ticket"])
    try:
        await call(tools, "ticket__create_ticket", title="一")
        await grant(c, "ticket", key, "none")
        denied = await call(tools, "ticket__create_ticket", title="二")
        sent = len(upstream.services["ticket"].seen)
        again = await call(tools, "ticket__create_ticket", title="三")
    finally:
        await sessions.aclose()
    assert "没有被授权使用 MCP 服务 ticket" in text_of(denied)
    assert text_of(again) == text_of(denied)
    assert len(upstream.services["ticket"].seen) == sent


async def test_timeout_of_side_effect_tool_says_result_unknown(env: Any) -> None:
    _app, _perms, c, key = env
    await grant(c, "ticket", key, "all")
    tools, _, sessions = await assemble(["mcp:ticket:create_ticket"], call_timeout_s=0.5)
    try:
        result = await call(tools, "ticket__create_ticket", title="slow")
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "结果未知" in text_of(result)


# ───────────────────────────── ACP、编辑器、保存


async def test_acp_gets_gateway_address_and_agent_key(env: Any) -> None:
    _app, _perms, _c, _key = env
    catalog = await gateway_catalog(GATEWAY, ttl_s=0, redis=None)
    access = await agent_access(GATEWAY, AGENT, name="x", owner=ALICE, ttl_s=0)
    servers, notices = await acp_mcp_servers(
        ["mcp:weather:get_weather", "mcp:ticket:create_ticket"],
        catalog,
        user_id=ALICE,
        digests={},
        drift_policy="warn",
        access=access,
    )
    assert [s["url"] for s in servers] == [f"{GATEWAY}/mcp/weather"]
    headers = {h["name"]: h["value"] for h in servers[0]["headers"]}
    assert headers["Authorization"] == f"Bearer {access.key}"
    assert [(k, d["server"], d["reason"]) for k, d in notices] == [
        (EventType.MCP_TOOL_SKIPPED, "ticket", "forbidden")
    ]


async def test_editor_lists_only_tools_the_agent_key_can_use(env: Any) -> None:
    from atlas_agent.config import get_settings
    from atlas_agent.services.meta import McpAgent, MetaService

    _app, _perms, c, key = env
    await grant(c, "ticket", key, "tools", ["create_ticket"])
    settings = get_settings().model_copy(update={"mcp_gateway_url": GATEWAY})
    service = MetaService(None, None, settings)  # type: ignore[arg-type]
    listed = await service.list_tools(mcp_agent=McpAgent(AGENT, "测试智能体", ALICE))
    mcp = {t.name: t.available for t in listed.data if t.kind == "mcp"}
    assert mcp == {"mcp:weather:get_weather": True, "mcp:ticket:create_ticket": True}
    assert listed.mcp_subject is not None
    assert str(listed.mcp_subject.uid) == key["subject_uid"]
    assert listed.mcp_subject.key_status == "active"

    # 新建、还没保存：只列公开服务
    unsaved = await service.list_tools(mcp_agent=McpAgent(None, "", ALICE))
    assert [t.name for t in unsaved.data if t.kind == "mcp"] == ["mcp:weather:get_weather"]
    assert unsaved.mcp_subject is not None and unsaved.mcp_subject.hidden_servers == 1


async def test_saving_new_mcp_tool_requires_grant(env: Any) -> None:
    from atlas_agent.config import get_settings
    from atlas_agent.db.models import Agent
    from atlas_agent.errors import InvalidReference
    from atlas_agent.schemas.agent import AgentSpecIn
    from atlas_agent.services.agent import AgentService

    _app, _perms, c, key = env
    settings = get_settings().model_copy(update={"mcp_gateway_url": GATEWAY})
    service = AgentService(None, settings)  # type: ignore[arg-type]
    base = {"model": {"model": "claude-sonnet-5"}, "system_prompt": "x"}
    previous = AgentSpecIn.model_validate({**base, "tool_names": ["mcp:weather:get_weather"]})
    spec = AgentSpecIn.model_validate(
        {**base, "tool_names": ["mcp:weather:get_weather", "mcp:ticket:create_ticket"]}
    )
    agent = Agent(id=AGENT, name="测试智能体", created_by=ALICE)
    with pytest.raises(InvalidReference, match="ticket__create_ticket"):
        await service._check_mcp(spec, previous=previous, agent=agent)
    await grant(c, "ticket", key, "all")
    await service._check_mcp(spec, previous=previous, agent=agent)
    # 新建的智能体（还没有 Key）：只能选公开服务
    await service._check_mcp(previous, previous=None, agent=None)
    with pytest.raises(InvalidReference, match="只能选公开的 MCP 服务"):
        await service._check_mcp(spec, previous=None, agent=None)

