"""Atlas 经自研 MCP 网关调 MCP（doc/mcp-design.html 第二版 §5、§6、§7）。

MCP 网关用真实现：atlas_mcpgateway 的 app 经 ASGITransport 跑在进程里（它自己的测试库
atlas_mcpgateway_test），用户中心换成内存替身；Higress 是后台线程里的替身（只认网关凭据）。
Atlas 的内部接口调用、MCP 客户端（FastMCP / httpx2）都经 ASGITransport 连到这个网关。

## 这组测试真正在守什么

  · 闸门一：权限主体没被授权、没有 Key 的服务装配时跳过并提示；编辑器只列能用的
  · 身份：主体 Key 在传输层，网关按 Key 写入身份，Pod / 模型改不了
  · 会话复用：同一个 Run 段对同一个服务只握手一次
  · 调用失败（§6.7）：参数不合法不发请求；Key 刚被重置自动重取重试一次；会话失效重建重试一次；
    无权在本 Run 段内记住、不再发请求；有副作用的工具超时要说「结果未知」
  · ACP：下发网关地址和主体 Key，没权限的服务不下发
  · 编辑器只列权限主体能用的服务；保存时新加的服务要有权限
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.providers.mcp.acp import acp_mcp_servers
from atlas_agent.providers.mcp.gateway import gateway_catalog, reset_gateway_cache, subject_access

# 夹具 gateway_environment / gateway / higress 在 conftest.py（来自 tests/gateway_env.py）
from tests.gateway_env import (
    ADMIN,
    ALICE,
    DIGITAL,
    GATEWAY,
    FakeHigress,
    FakeService,
    as_user,
    assemble,
    call,
    register,
    text_of,
)
from tests.gateway_env import approve as approve_tool
from tests.gateway_env import issue_key as issue

pytestmark = pytest.mark.usefixtures("gateway_environment")


@pytest.fixture
async def env(gateway: Any, higress: FakeHigress) -> Any:
    """weather（只读、不要求复核）；ticket（create_ticket 已批准、delete_ticket 待复核）。"""
    weather = FakeService()
    weather.tool(
        "get_weather",
        "查询某个城市今天的天气。",
        lambda args, h: f"{args['city']}：小雨（提交人={h.get('x-auth-uid')}）",
        properties={"city": {"type": "string"}},
        read_only=True,
    )
    ticket = FakeService()

    async def create(args: dict[str, Any], h: dict[str, str]) -> str:
        if args.get("title") == "slow":
            await asyncio.sleep(1.5)
        return f"T-001 {args['title']} 提交人={h.get('x-auth-uid')} 使用者={h.get('x-uid')}"

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
    higress.services.update({"weather": weather, "ticket": ticket})
    app, perms, c = gateway
    await register(c, "weather", higress=higress)
    await register(c, "ticket", review=True, higress=higress)
    await approve_tool(c, "ticket", "create_ticket")
    return app, perms, c


async def _issue_key(c: httpx.AsyncClient, subject: Any) -> None:
    await issue(c, subject)


_assemble = assemble
_call = call
_text = text_of


# ───────────────────────────── 装配：闸门一


async def test_unauthorized_and_keyless_services_are_skipped_with_notice(env: Any) -> None:
    _app, perms, c = env
    perms.allow("weather", str(ALICE))
    tools, notices, _ = await _assemble(
        ["mcp:weather:get_weather", "mcp:ticket:create_ticket"], ALICE
    )
    assert tools == []  # weather 有权限但没 Key；ticket 没权限
    reasons = {d["server"]: d["reason"] for k, d in notices if k is EventType.MCP_TOOL_SKIPPED}
    assert reasons == {"weather": "no_key", "ticket": "forbidden"}

    await _issue_key(c, ALICE)
    tools, notices, _ = await _assemble(["mcp:weather:get_weather"], ALICE)
    assert [t.name for t in tools] == ["weather__get_weather"]
    assert notices == []


async def test_pending_review_tool_is_skipped(env: Any) -> None:
    _app, perms, c = env
    perms.allow("ticket", str(ALICE))
    await _issue_key(c, ALICE)
    tools, notices, _ = await _assemble(
        ["mcp:ticket:create_ticket", "mcp:ticket:delete_ticket"], ALICE
    )
    assert [t.name for t in tools] == ["ticket__create_ticket"]
    drift = [d for k, d in notices if k is EventType.MCP_TOOL_DRIFT]
    assert drift[0]["tool"] == "mcp:ticket:delete_ticket"
    assert drift[0]["action"] == "pending_review"


# ───────────────────────────── 调用


async def test_call_carries_subject_key_and_reuses_session(env: Any, higress: FakeHigress) -> None:
    _app, perms, c = env
    perms.allow("weather", str(DIGITAL))
    await _issue_key(c, DIGITAL)
    tools, _, sessions = await _assemble(["mcp:weather:get_weather"], DIGITAL)
    try:
        for city in ("上海", "北京", "广州"):
            result = await _call(tools, "weather__get_weather", city=city)
            assert f"提交人={DIGITAL}" in _text(result)
    finally:
        await sessions.aclose()
    methods = [m for m, _ in higress.services["weather"].seen]
    # 一个会话：握手一次，SDK 为校验结果补拉一次列表，之后每次调用一个请求（§6.3）
    assert methods.count("initialize") == 1
    assert methods.count("tools/call") == 3


async def test_invalid_arguments_go_back_to_model_without_request(
    env: Any, higress: FakeHigress
) -> None:
    _app, perms, c = env
    perms.allow("ticket", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:ticket:create_ticket"], ALICE)
    try:
        result = await _call(tools, "ticket__create_ticket", titel="拼错了")
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "参数不合法" in _text(result)
    assert "titel" in _text(result)
    assert higress.services["ticket"].seen == []


async def test_reset_key_is_refetched_and_retried_once(env: Any) -> None:
    _app, perms, c = env
    perms.allow("weather", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:weather:get_weather"], ALICE)
    try:
        assert "小雨" in _text(await _call(tools, "weather__get_weather", city="上海"))
        key_id = (await c.get("/api/v1/keys", headers=as_user(ADMIN))).json()[0]["id"]
        await c.post(f"/api/v1/keys/{key_id}/reset", headers=as_user(ADMIN))
        result = await _call(tools, "weather__get_weather", city="上海")
    finally:
        await sessions.aclose()
    assert result.status != "error", _text(result)
    assert "小雨" in _text(result)


async def test_disabled_key_is_reported_and_remembered(env: Any, higress: FakeHigress) -> None:
    _app, perms, c = env
    perms.allow("weather", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:weather:get_weather"], ALICE)
    try:
        await _call(tools, "weather__get_weather", city="上海")
        key_id = (await c.get("/api/v1/keys", headers=as_user(ADMIN))).json()[0]["id"]
        await c.post(f"/api/v1/keys/{key_id}/disable", headers=as_user(ADMIN))
        first = await _call(tools, "weather__get_weather", city="上海")
        calls_before = len(higress.services["weather"].seen)
        second = await _call(tools, "weather__get_weather", city="北京")
    finally:
        await sessions.aclose()
    assert first.status == "error"
    assert "API Key 已失效或已停用" in _text(first)
    assert "不要重试" in _text(first)
    # 本 Run 段内记住：第二次不再发请求
    assert _text(second) == _text(first)
    assert len(higress.services["weather"].seen) == calls_before


async def test_revoked_grant_mid_run_is_remembered_per_service(
    env: Any, higress: FakeHigress
) -> None:
    app, perms, c = env
    perms.allow("weather", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:weather:get_weather"], ALICE)
    try:
        await _call(tools, "weather__get_weather", city="上海")
        perms.grants["weather"].pop(str(ALICE))
        app.state.usable.forget()
        denied = await _call(tools, "weather__get_weather", city="上海")
        sent = len(higress.services["weather"].seen)
        again = await _call(tools, "weather__get_weather", city="北京")
    finally:
        await sessions.aclose()
    assert "没有被授权使用 MCP 服务 weather" in _text(denied)
    assert _text(again) == _text(denied)
    assert len(higress.services["weather"].seen) == sent


async def test_expired_session_is_rebuilt_and_retried(env: Any, higress: FakeHigress) -> None:
    _app, perms, c = env
    perms.allow("weather", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:weather:get_weather"], ALICE)
    try:
        await _call(tools, "weather__get_weather", city="上海")
        higress.services["weather"].sessions.clear()  # 上游重启，会话没了
        result = await _call(tools, "weather__get_weather", city="北京")
    finally:
        await sessions.aclose()
    assert result.status != "error", _text(result)
    assert "北京" in _text(result)
    assert [m for m, _ in higress.services["weather"].seen].count("initialize") == 2


async def test_timeout_of_side_effect_tool_says_result_unknown(env: Any) -> None:
    _app, perms, c = env
    perms.allow("ticket", str(ALICE))
    await _issue_key(c, ALICE)
    tools, _, sessions = await _assemble(["mcp:ticket:create_ticket"], ALICE, call_timeout_s=0.5)
    try:
        result = await _call(tools, "ticket__create_ticket", title="slow")
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "结果未知" in _text(result)


# ───────────────────────────── ACP、编辑器、指纹


async def test_acp_gets_gateway_address_and_subject_key(env: Any) -> None:
    _app, perms, c = env
    perms.allow("weather", str(ALICE))
    await _issue_key(c, ALICE)
    catalog = await gateway_catalog(GATEWAY, ttl_s=0, redis=None)
    access = await subject_access(GATEWAY, ALICE, ttl_s=0)
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


async def test_editor_lists_only_services_the_subject_can_use(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlas_agent.config import get_settings
    from atlas_agent.services.meta import McpSubject, MetaService

    _app, perms, _c = env
    perms.allow("ticket", str(DIGITAL))
    settings = get_settings().model_copy(update={"mcp_gateway_url": GATEWAY})
    service = MetaService(None, None, settings)  # type: ignore[arg-type]
    listed = await service.list_tools(mcp_subject=McpSubject(kind="agent", uid=DIGITAL))
    mcp = {t.name: t.available for t in listed.data if t.kind == "mcp"}
    assert mcp == {"mcp:ticket:create_ticket": True, "mcp:ticket:delete_ticket": False}
    assert listed.mcp_subject is not None
    assert listed.mcp_subject.hidden_servers == 1
    assert listed.mcp_subject.key_status == "none"

    unsaved = await service.list_tools(mcp_subject=McpSubject(kind="agent", uid=None))
    assert [t for t in unsaved.data if t.kind == "mcp"] == []


async def test_saving_new_mcp_service_requires_permission(env: Any) -> None:
    from atlas_agent.config import get_settings
    from atlas_agent.errors import InvalidReference
    from atlas_agent.schemas.agent import AgentSpecIn
    from atlas_agent.services.agent import AgentService

    app, perms, _c = env
    settings = get_settings().model_copy(update={"mcp_gateway_url": GATEWAY})
    service = AgentService(None, settings)  # type: ignore[arg-type]
    base = {"model": {"model": "claude-sonnet-5"}, "system_prompt": "x"}
    previous = AgentSpecIn.model_validate({**base, "tool_names": ["mcp:weather:get_weather"]})
    spec = AgentSpecIn.model_validate(
        {**base, "tool_names": ["mcp:weather:get_weather", "mcp:ticket:create_ticket"]}
    )
    with pytest.raises(InvalidReference, match="ticket"):
        await service._check_mcp(spec, previous=previous, subject=ALICE)
    # 旧版本已经选了的不重判（alice 对 weather 也没权限）。两边的判权缓存都清掉：授权刚到
    perms.allow("ticket", str(ALICE))
    app.state.usable.forget()
    reset_gateway_cache()
    await service._check_mcp(spec, previous=previous, subject=ALICE)
    # 以 Agent 为权限、还没有数字员工：新加 MCP 服务一律拒绝
    with pytest.raises(InvalidReference, match="数字员工"):
        await service._check_mcp(spec, previous=None, subject=None)
