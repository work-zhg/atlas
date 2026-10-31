"""MCP 工具接入：Atlas 的装配与调用链（doc/mcp-design.html 第二版 §6）。

全部经真实的 MCP 网关（进程内）→ 上游 MCP 服务的替身（tests/gateway_env.py）。mock 掉 MCP 协议就
等于什么都没测：这块的风险在协议握手、工具 schema 转换和错误转换上。网关自身的认 Key、判权在它自己
的测试里（services/mcpgateway/tests），经网关的权限与失败处理见 test_mcp_gateway.py。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.providers.mcp import (
    CallContext,
    McpServerConfig,
    MissingTool,
    model_name,
    parse_tool_id,
    tool_id,
)
from pydantic import ValidationError

from tests.fake_uc import AS_USER, TEST_USER
from tests.gateway_env import (
    GATEWAY,
    FakeMcp,
    FakeMcpService,
    ToolFailure,
    agent_key,
    assemble,
    call,
    grant,
    register,
    text_of,
)

pytestmark = pytest.mark.usefixtures("gateway_environment")

SRV = "srv"


def _service() -> FakeMcpService:
    srv = FakeMcpService()
    num = {"type": "integer"}
    srv.tool(
        "add", "两数相加。", lambda a, _h: str(a["a"] + a["b"]), properties={"a": num, "b": num}
    )
    srv.tool(
        "read_file", "与内置文件工具同名 —— 模型侧名有命名空间，不会撞车。", lambda a, _h: "remote"
    )

    async def slow(args: dict[str, Any], _h: dict[str, str]) -> str:
        await asyncio.sleep(float(args["seconds"]))
        return "done"

    srv.tool("slow", "睡一会儿再返回。", slow, properties={"seconds": {"type": "number"}})
    srv.tool("big", "返回 n 个字符。", lambda a, _h: "x" * int(a["n"]), properties={"n": num})
    srv.tool(
        "whoami",
        "回显身份请求头。",
        lambda _a, h: f"user={h.get('x-uid')} run={h.get('x-run-id')} auth={h.get('x-auth-uid')}",
    )
    srv.tool(
        "lookup", "声明了只读的工具。", lambda a, _h: "value", annotations={"readOnlyHint": True}
    )

    def fail(args: dict[str, Any], _h: dict[str, str]) -> str:
        raise ToolFailure(str(args["reason"]))

    srv.tool("fail", "总是失败（isError）。", fail, properties={"reason": {"type": "string"}})
    return srv


@pytest.fixture
async def env(gateway: Any, upstream: FakeMcp) -> Any:
    """私有服务 srv 已登记，智能体的 Key 有服务级授权。→ (app, perms, c, key)。"""
    app, perms, c = gateway
    upstream.services[SRV] = _service()
    await register(c, SRV, upstream=upstream)
    key = await agent_key(c)
    await grant(c, SRV, key, "all")
    return app, perms, c, key


# ---------------------------------------------------------------- 装配


async def test_loads_only_requested_tools_with_namespace(env: Any) -> None:
    """一个服务可能有十几个工具，用户只勾了其中几个；模型看到的名字带前缀。"""
    tools, _, sessions = await assemble(
        [tool_id(SRV, "add"), tool_id(SRV, "read_file"), "write_todos", "filesystem"]
    )
    await sessions.aclose()
    # ★ read_file 与内置文件工具同名 —— 此前模型看到的就是裸 read_file
    assert sorted(t.name for t in tools) == [f"{SRV}__add", f"{SRV}__read_file"]


async def test_tool_actually_executes(env: Any) -> None:
    """★ 真的调过去并拿到结果 —— 改了 LangChain 侧名字，线上名必须不受影响。"""
    tools, _, sessions = await assemble([tool_id(SRV, "add")])
    try:
        assert text_of(await call(tools, f"{SRV}__add", a=17, b=25)) == "42"
    finally:
        await sessions.aclose()


async def test_unknown_tool_raises(env: Any) -> None:
    """★ 勾了服务上没有的工具要明确报错，而不是少装一个就默默跑（§13.2）。"""
    with pytest.raises(MissingTool, match="nope"):
        await assemble([tool_id(SRV, "nope")])


async def test_read_only_annotation_reaches_approval(env: Any) -> None:
    """★ 审批按 metadata["readOnlyHint"] 判只读。langchain.mcp 把注解收在
    metadata["mcp"] 下、键是 snake_case；装配时要平铺回来，否则只读工具全被当成外部。"""
    from atlas_agent.domain.tool_registry import tool_effects
    from atlas_engine.contracts import ToolEffect

    tools, _, sessions = await assemble([tool_id(SRV, "lookup"), tool_id(SRV, "add")])
    await sessions.aclose()
    effects = tool_effects(tools)
    assert effects[f"{SRV}__lookup"] is ToolEffect.READ
    assert effects[f"{SRV}__add"] is ToolEffect.EXTERNAL


# ---------------------------------------------------------------- 调用期


async def test_identity_headers_reach_the_backend(env: Any) -> None:
    """使用者（X-Uid）和 X-Run-Id 照转、只作审计；权限主体（X-Auth-Uid）由网关按 Key 写入。"""
    *_, key = env
    run = uuid4()
    caller = CallContext(run_id=run, thread_id=uuid4(), user_id=UUID(TEST_USER))
    tools, _, sessions = await assemble([tool_id(SRV, "whoami")], call_ctx=caller)
    try:
        out = text_of(await call(tools, f"{SRV}__whoami"))
    finally:
        await sessions.aclose()
    assert out == f"user={TEST_USER} run={run} auth={key['subject_uid']}"


async def test_timeout_goes_to_model_not_run(env: Any) -> None:
    """★ 超时变成给模型的工具结果，而不是打死整个 run；有副作用的要说结果未知。"""
    tools, _, sessions = await assemble([tool_id(SRV, "slow")], call_timeout_s=0.5)
    started = time.monotonic()
    try:
        out = text_of(await call(tools, f"{SRV}__slow", seconds=5))
    finally:
        await sessions.aclose()
    assert time.monotonic() - started < 3
    assert "超时（0.5s）" in out
    assert "结果未知" in out


def test_per_server_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(name="x", url="https://x/mcp", call_timeout_s=0)


async def test_cancellation_is_not_swallowed(env: Any) -> None:
    """★ 取消 run 时，进行中的 MCP 调用必须立即中止 —— 错误转换不能把
    CancelledError 当成「服务不可用」吞掉，否则取消按钮失效。"""
    tools, _, sessions = await assemble([tool_id(SRV, "slow")])
    task = asyncio.create_task(call(tools, f"{SRV}__slow", seconds=10))
    await asyncio.sleep(0.5)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3
    await sessions.aclose()


async def test_upstream_down_at_call_time_goes_to_model(env: Any) -> None:
    """★ 定义在、上游宕了：装配成功，调用返回错误文本，不抛异常。"""
    from atlas_mcpgateway.db.models import McpService
    from atlas_mcpgateway.db.session import get_sessionmaker
    from sqlalchemy import select

    tools, _, sessions = await assemble([tool_id(SRV, "add")])
    async with get_sessionmaker()() as session:
        svc = await session.scalar(select(McpService).where(McpService.name == SRV))
        assert svc is not None
        svc.connection = {**svc.connection, "url": "http://127.0.0.1:9/mcp"}
        await session.commit()
    try:
        result = await call(tools, f"{SRV}__add", a=1, b=2)
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "暂时不可用" in text_of(result)


async def test_business_error_goes_to_model(env: Any) -> None:
    """服务返回的 isError 原样交给模型（它要看到内容），不打断 run。"""
    tools, _, sessions = await assemble([tool_id(SRV, "fail")])
    try:
        result = await call(tools, f"{SRV}__fail", reason="库存不足")
    finally:
        await sessions.aclose()
    assert result.status == "error"
    assert "库存不足" in text_of(result)


async def test_huge_result_is_clipped(env: Any) -> None:
    tools, _, sessions = await assemble([tool_id(SRV, "big")], max_result_chars=1000)
    try:
        out = text_of(await call(tools, f"{SRV}__big", n=50_000))
    finally:
        await sessions.aclose()
    assert "结果过长" in out
    assert len(out) < 2000


# ---------------------------------------------------------------- 命名


def test_tool_id_roundtrip() -> None:
    assert tool_id("srv", "do_thing") == "mcp:srv:do_thing"
    assert parse_tool_id("mcp:srv:do_thing") == ("srv", "do_thing")


def test_parse_ignores_builtin_tools() -> None:
    assert parse_tool_id("write_todos") is None
    assert parse_tool_id("mcp:") is None
    assert parse_tool_id("mcp:onlyserver") is None


def test_server_name_must_not_contain_underscore() -> None:
    """★ 服务名含 `_` 的话，`server__tool` 的分隔点就不唯一了。"""
    with pytest.raises(ValidationError):
        McpServerConfig(name="my_server", url="https://x/mcp")
    McpServerConfig(name="my-server", url="https://x/mcp")


def test_model_name_rejects_illegal_tool_names() -> None:
    assert model_name("srv", "search") == "srv__search"
    assert model_name("srv", "has space") is None
    assert model_name("srv", "x" * 70) is None


# ---------------------------------------------------------------- 目录端点


def _app_with_gateway(url: str) -> Any:
    from atlas_agent.config import Settings, get_settings
    from atlas_agent.main import create_app

    patched = Settings(**{**get_settings().model_dump(), "mcp_gateway_url": url})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: patched
    return app


async def test_catalog_endpoint_includes_mcp(gateway: Any, upstream: FakeMcp) -> None:
    """★ 端到端：/v1/tools 里同时有内置工具与 MCP 工具。新建的智能体还没有 Key，只列公开服务。"""
    _app, _perms, c = gateway
    upstream.services[SRV] = _service()
    upstream.services["secret"] = _service()
    await register(c, SRV, visibility="public", upstream=upstream)
    await register(c, "secret", upstream=upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app_with_gateway(GATEWAY)),
        base_url="http://test",
        headers=AS_USER,
    ) as client:
        res = await client.get("/v1/tools")
    assert res.status_code == 200, res.text
    body = res.json()
    by_name = {t["name"]: t for t in body["data"]}
    assert "write_todos" in by_name, "内置工具不见了"
    add = by_name.get(tool_id(SRV, "add"))
    assert add is not None, "MCP 工具没并进目录"
    assert add["model_tool_names"] == [f"{SRV}__add"]
    assert tool_id("secret", "add") not in by_name  # 私有服务：新建的智能体选不了
    subject = body["mcp_subject"]
    assert (subject["kind"], subject["key_status"], subject["hidden_servers"]) == (
        "agent",
        "none",
        1,
    )


async def test_unreachable_gateway_does_not_break_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ MCP 网关挂了（且没有缓存），目录照常返回内置工具，并说明 MCP 暂时不可用。"""
    from atlas_agent.providers.mcp import gateway as gateway_module

    gateway_module.reset_gateway_cache()

    async def no_copy(_redis: Any) -> None:
        return None

    # 开发 Redis 里可能留着别的用例写的元数据副本：这里要的是「从没取到过」
    monkeypatch.setattr(gateway_module, "_read_copy", no_copy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app_with_gateway("http://127.0.0.1:9")),
        base_url="http://test",
        headers=AS_USER,
    ) as client:
        res = await client.get("/v1/tools")
    assert res.status_code == 200, res.text
    body = res.json()
    assert all(t["kind"] != "mcp" for t in body["data"])
    assert "MCP 网关暂时不可用" in body["mcp_subject"]["notes"][0]
