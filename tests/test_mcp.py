"""MCP 工具接入（MCP 详设）。

对**真实的本地 MCP server** 测（tests/mcp_server.py，streamable_http）——
mock 掉 MCP 协议就等于什么都没测：这块的风险全在协议握手与工具 schema 转换上。

目录缓存用真实 Redis。每个测试用独立的 server 名，互不串缓存。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from atlas_server.config import get_settings
from atlas_server.domain.mcp_naming import approval_target
from atlas_server.providers.mcp import (
    CallContext,
    McpServerConfig,
    McpUnavailable,
    MissingCredential,
    MissingTool,
    NativeMcpTools,
    SettingsCatalog,
    model_name,
    parse_tool_id,
    resolve_env_refs,
    tool_id,
)
from atlas_server.redisx import make_redis
from pydantic import ValidationError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _name() -> str:
    """每个测试一个 server 名：缓存按 server 名存，共用会互相污染。"""
    return f"t{uuid.uuid4().hex[:10]}"


CTX = CallContext(run_id=uuid.uuid4(), thread_id=uuid.uuid4(), user_id=uuid.uuid4())


@pytest.fixture(scope="module")
def mcp_port() -> Iterator[int]:
    """起一个真实的 MCP server 子进程。

    不塞进测试进程：它自带 event loop 与 ASGI 栈，会和 pytest-asyncio 打架。
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.mcp_server", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等到端口真的能连上，而不是固定 sleep —— 固定值在慢机器上必然 flaky
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("本地 MCP server 未能启动")

    yield port
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
async def redis() -> AsyncIterator[object]:
    client = make_redis(get_settings())
    yield client
    async for key in client.scan_iter("atlas:mcp:*:t*"):
        await client.delete(key)
    await client.aclose()


def _live(port: int, name: str) -> McpServerConfig:
    return McpServerConfig(name=name, url=f"http://127.0.0.1:{port}/mcp")


def _dead(name: str) -> McpServerConfig:
    return McpServerConfig(name=name, url=f"http://127.0.0.1:{_free_port()}/mcp")


def _tools(catalog: SettingsCatalog, **kw: object) -> NativeMcpTools:
    return NativeMcpTools(catalog, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 目录与缓存


async def test_discovers_tools_with_model_names(mcp_port: int, redis: object) -> None:
    name = _name()
    snap = await SettingsCatalog([_live(mcp_port, name)], redis).snapshot(name)  # type: ignore[arg-type]

    add = snap.tool("add")
    assert add is not None
    assert "相加" in add.description  # 描述来自 server 端 docstring，编辑器要显示它
    assert add.model_name == f"{name}__add"
    assert add.usable
    assert len(add.digest) == 64


async def test_cached_snapshot_needs_no_network(mcp_port: int, redis: object) -> None:
    """★ 装配期读缓存：server 宕了，只要缓存在，照样能装配（调用期再报错）。"""
    name = _name()
    await SettingsCatalog([_live(mcp_port, name)], redis).snapshot(name)  # type: ignore[arg-type]

    down = SettingsCatalog([_dead(name)], redis)  # type: ignore[arg-type]
    snap = await down.snapshot(name)
    assert snap.tool("add") is not None
    assert snap.error is None


async def test_stale_cache_survives_failed_refresh(mcp_port: int, redis: object) -> None:
    """★ 软过期后刷新失败：保留旧定义并记下原因，而不是让工具消失。"""
    name = _name()
    await SettingsCatalog([_live(mcp_port, name)], redis).snapshot(name)  # type: ignore[arg-type]

    down = SettingsCatalog([_dead(name)], redis, soft_ttl_s=0)  # type: ignore[arg-type]
    snap = await down.snapshot(name)
    assert snap.tool("add") is not None
    assert snap.error and "ConnectError" in snap.error


async def test_cold_miss_on_dead_server_raises(redis: object) -> None:
    name = _name()
    with pytest.raises(McpUnavailable, match=name):
        await SettingsCatalog([_dead(name)], redis).snapshot(name)  # type: ignore[arg-type]


async def test_unregistered_server_raises(redis: object) -> None:
    with pytest.raises(McpUnavailable, match="未注册或已禁用"):
        await SettingsCatalog([], redis).snapshot("nope")  # type: ignore[arg-type]


async def test_disabled_server_is_not_listed(mcp_port: int, redis: object) -> None:
    name = _name()
    disabled = McpServerConfig(name=name, url=f"http://127.0.0.1:{mcp_port}/mcp", enabled=False)
    assert SettingsCatalog([disabled], redis).servers() == []  # type: ignore[arg-type]


async def test_overlong_description_is_unusable(mcp_port: int, redis: object) -> None:
    """★ 描述超长本身就是可疑信号：标不可用，不让它悄悄进模型上下文。"""
    name = _name()
    snap = await SettingsCatalog([_live(mcp_port, name)], redis).snapshot(name)  # type: ignore[arg-type]
    verbose = snap.tool("verbose")
    assert verbose is not None and not verbose.usable
    assert "描述过长" in verbose.issues[0]


# ---------------------------------------------------------------- native 装配


async def test_loads_only_requested_tools_with_namespace(mcp_port: int, redis: object) -> None:
    """一个 server 可能有十几个工具，用户只勾了其中几个；模型看到的名字带前缀。"""
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    tools = await _tools(catalog).tools_for(
        [tool_id(name, "add"), tool_id(name, "read_file"), "write_todos", "filesystem"], CTX
    )
    # ★ read_file 与内置文件工具同名 —— 此前模型看到的就是裸 read_file
    assert sorted(t.name for t in tools) == [f"{name}__add", f"{name}__read_file"]


async def test_tool_actually_executes(mcp_port: int, redis: object) -> None:
    """★ 真的调过去并拿到结果 —— 改了 LangChain 侧名字，线上名必须不受影响。"""
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    [add] = await _tools(catalog).tools_for([tool_id(name, "add")], CTX)
    assert "42" in str(await add.ainvoke({"a": 17, "b": 25}))


async def test_unknown_tool_raises(mcp_port: int, redis: object) -> None:
    """★ 勾了 server 上没有的工具要明确报错，而不是少装一个就默默跑（§13.2）。"""
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    with pytest.raises(MissingTool, match="nope"):
        await _tools(catalog).tools_for([tool_id(name, "nope")], CTX)


async def test_unusable_tool_raises(mcp_port: int, redis: object) -> None:
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    with pytest.raises(MissingTool, match="不可用"):
        await _tools(catalog).tools_for([tool_id(name, "verbose")], CTX)


# ---------------------------------------------------------------- 调用期


async def test_identity_headers_are_sent(mcp_port: int, redis: object) -> None:
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    [who] = await _tools(catalog).tools_for([tool_id(name, "whoami")], CTX)
    out = str(await who.ainvoke({}))
    assert f"user={CTX.user_id}" in out
    assert f"run={CTX.run_id}" in out


async def test_timeout_goes_to_model_not_run(mcp_port: int, redis: object) -> None:
    """★ 超时变成给模型的工具错误，而不是打死整个 run。"""
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    [slow] = await _tools(catalog, call_timeout_s=0.5).tools_for([tool_id(name, "slow")], CTX)
    started = time.monotonic()
    out = str(await slow.ainvoke({"seconds": 5}))
    assert time.monotonic() - started < 3
    assert "超时" in out


async def test_per_server_timeout_overrides_global(mcp_port: int, redis: object) -> None:
    """★ server 级的 call_timeout_s 优先于全局：全局 60s，这台只给 0.5s。"""
    name = _name()
    cfg = McpServerConfig(name=name, url=f"http://127.0.0.1:{mcp_port}/mcp", call_timeout_s=0.5)
    catalog = SettingsCatalog([cfg], redis)  # type: ignore[arg-type]
    [slow] = await _tools(catalog, call_timeout_s=60).tools_for([tool_id(name, "slow")], CTX)
    started = time.monotonic()
    out = str(await slow.ainvoke({"seconds": 5}))
    assert time.monotonic() - started < 3
    assert "超时（0.5s）" in out


def test_per_server_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(name="x", url="https://x/mcp", call_timeout_s=0)


async def test_cancellation_is_not_swallowed(mcp_port: int, redis: object) -> None:
    """★ 取消 run 时，进行中的 MCP 调用必须立即中止 —— 错误转换不能把
    CancelledError 当成「服务不可用」吞掉，否则取消按钮失效。"""
    import asyncio

    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    [slow] = await _tools(catalog).tools_for([tool_id(name, "slow")], CTX)
    task = asyncio.create_task(slow.ainvoke({"seconds": 10}))
    await asyncio.sleep(0.5)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3


async def test_dead_server_at_call_time_goes_to_model(mcp_port: int, redis: object) -> None:
    """★ 缓存在、server 宕了：装配成功，调用返回错误文本，不抛异常。"""
    name = _name()
    await SettingsCatalog([_live(mcp_port, name)], redis).snapshot(name)  # type: ignore[arg-type]
    down = SettingsCatalog([_dead(name)], redis)  # type: ignore[arg-type]
    [add] = await _tools(down).tools_for([tool_id(name, "add")], CTX)
    out = str(await add.ainvoke({"a": 1, "b": 2}))
    assert "暂时不可用" in out


async def test_huge_result_is_clipped(mcp_port: int, redis: object) -> None:
    name = _name()
    catalog = SettingsCatalog([_live(mcp_port, name)], redis)  # type: ignore[arg-type]
    [big] = await _tools(catalog, max_result_chars=1000).tools_for([tool_id(name, "big")], CTX)
    out = str(await big.ainvoke({"n": 50_000}))
    assert "结果过长" in out
    assert len(out) < 2000


# ---------------------------------------------------------------- 凭据


def test_env_placeholder_resolved() -> None:
    os.environ["ATLAS_TEST_MCP_TOKEN"] = "secret-123"
    try:
        assert resolve_env_refs("Bearer ${ATLAS_TEST_MCP_TOKEN}") == "Bearer secret-123"
    finally:
        del os.environ["ATLAS_TEST_MCP_TOKEN"]


def test_missing_env_raises_instead_of_empty() -> None:
    """★ 不能把缺失的环境变量当空串：那会拿空 token 去连，报错指向「认证失败」。"""
    with pytest.raises(MissingCredential, match="ATLAS_TEST_ABSENT"):
        resolve_env_refs("Bearer ${ATLAS_TEST_ABSENT}")


def test_credentials_never_appear_in_config() -> None:
    """★ §14：凭据不落配置。配置对象里只该有占位符。"""
    cfg = McpServerConfig(name="x", url="https://x/mcp", headers={"Authorization": "Bearer ${TOK}"})
    dumped = cfg.model_dump_json()
    assert "${TOK}" in dumped
    assert "secret" not in dumped.lower()


# ---------------------------------------------------------------- 命名


def test_tool_id_roundtrip() -> None:
    assert tool_id("srv", "do_thing") == "mcp:srv:do_thing"
    assert parse_tool_id("mcp:srv:do_thing") == ("srv", "do_thing")


def test_parse_ignores_builtin_tools() -> None:
    assert parse_tool_id("write_todos") is None
    assert parse_tool_id("mcp:") is None
    assert parse_tool_id("mcp:onlyserver") is None


def test_server_name_must_not_contain_underscore() -> None:
    """★ server 名含 `_` 的话，`server__tool` 的分隔点就不唯一了。"""
    with pytest.raises(ValidationError):
        McpServerConfig(name="my_server", url="https://x/mcp")
    McpServerConfig(name="my-server", url="https://x/mcp")


def test_model_name_rejects_illegal_tool_names() -> None:
    assert model_name("srv", "search") == "srv__search"
    assert model_name("srv", "has space") is None
    assert model_name("srv", "x" * 70) is None


def test_approval_target_translates_spec_ids() -> None:
    """★ 编辑器此前提示手填 mcp:server:tool —— 那种写法从没匹配上过，现在翻译过来。"""
    assert approval_target("mcp:github:search") == "github__search"
    assert approval_target("github__search") == "github__search"
    assert approval_target("execute") == "execute"


# ---------------------------------------------------------------- 目录端点


async def test_catalog_endpoint_includes_mcp(mcp_port: int, redis: object) -> None:
    """★ 端到端：/v1/tools 里同时有内置工具与 MCP 工具，且 MCP 工具可选作审批目标。"""
    from atlas_server.config import Settings
    from atlas_server.main import create_app
    from httpx import ASGITransport

    name = _name()
    base = get_settings()
    patched = Settings(**{**base.model_dump(), "mcp_servers": [_live(mcp_port, name)]})

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: patched
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.get("/v1/tools")

    assert res.status_code == 200, res.text
    by_name = {t["name"]: t for t in res.json()["data"]}
    assert "write_todos" in by_name, "内置工具不见了"
    add = by_name.get(tool_id(name, "add"))
    assert add is not None, "MCP 工具没并进目录"
    assert add["model_tool_names"] == [f"{name}__add"]
    assert by_name[tool_id(name, "verbose")]["available"] is False


async def test_unreachable_server_does_not_break_catalog(redis: object) -> None:
    """★ 一台 server 挂了（且无缓存），目录照常返回其余内容。"""
    from atlas_server.config import Settings
    from atlas_server.main import create_app
    from httpx import ASGITransport

    base = get_settings()
    patched = Settings(**{**base.model_dump(), "mcp_servers": [_dead(_name())]})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: patched
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/v1/tools")
    assert res.status_code == 200, res.text
    assert all(t["kind"] != "mcp" for t in res.json()["data"])
