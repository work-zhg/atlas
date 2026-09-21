"""P7 · MCP 工具接入。

对**真实的本地 MCP server** 测（tests/mcp_server.py，streamable_http）——
mock 掉 MCP 协议就等于什么都没测：这块的风险全在协议握手与工具 schema 转换上。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from atlas_server.services.mcp import (
    McpServerConfig,
    McpService,
    MissingCredential,
    MissingTool,
    parse_tool_id,
    resolve_env_refs,
    tool_id,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def mcp_port() -> int:
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
def service(mcp_port: int) -> McpService:
    return McpService([McpServerConfig(name="local", url=f"http://127.0.0.1:{mcp_port}/mcp")])


# ---------------------------------------------------------------- 发现与调用


async def test_discovers_tools(service: McpService) -> None:
    """★ /v1/tools 要能列出远程 server 上的工具（§11）。"""
    catalog = await service.list_tools()
    names = {t["name"] for t in catalog}

    assert "mcp:local:add" in names
    assert "mcp:local:echo" in names
    # 描述来自 server 端的 docstring，前端编辑器要显示它
    add = next(t for t in catalog if t["name"] == "mcp:local:add")
    assert "相加" in add["description"]


async def test_loads_only_requested_tools(service: McpService) -> None:
    """一个 server 可能有十几个工具，用户只勾了其中几个。"""
    tools = await service.tools_for(["mcp:local:add", "write_todos", "filesystem"])
    assert [t.name for t in tools] == ["add"]


async def test_tool_actually_executes(service: McpService) -> None:
    """★ 真的调过去并拿到结果 —— 这条挂了说明 schema 转换或握手有问题。"""
    tools = await service.tools_for(["mcp:local:add"])
    result = await tools[0].ainvoke({"a": 17, "b": 25})
    assert "42" in str(result)


async def test_unknown_tool_raises(service: McpService) -> None:
    """★ 勾了 server 上没有的工具要明确报错。

    静默少装一个工具，agent 就会因为「找不到工具」在几轮之后才失败，
    报错离病根很远（§13.2）。
    """
    with pytest.raises(MissingTool, match="nope"):
        await service.tools_for(["mcp:local:nope"])


async def test_unreachable_server_does_not_break_catalog() -> None:
    """★ 一台 server 挂了，其余的工具照常列出。

    目录是编辑器的数据源；一台远程服务抽风就整页空白是不可接受的。
    """
    port = _free_port()  # 没人监听
    service = McpService(
        [
            McpServerConfig(name="dead", url=f"http://127.0.0.1:{port}/mcp"),
        ]
    )
    assert await service.list_tools() == []


# ---------------------------------------------------------------- 凭据


def test_env_placeholder_resolved() -> None:
    os.environ["ATLAS_TEST_MCP_TOKEN"] = "secret-123"
    try:
        assert resolve_env_refs("Bearer ${ATLAS_TEST_MCP_TOKEN}") == "Bearer secret-123"
    finally:
        del os.environ["ATLAS_TEST_MCP_TOKEN"]


def test_missing_env_raises_instead_of_empty() -> None:
    """★ 不能把缺失的环境变量当空串。

    那会拿一个空 token 去连，服务端回 401，报错指向「认证失败」
    而不是「你没配这个环境变量」。
    """
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


def test_disabled_server_is_skipped(mcp_port: int) -> None:
    service = McpService(
        [McpServerConfig(name="local", url=f"http://127.0.0.1:{mcp_port}/mcp", enabled=False)]
    )
    assert not service.configured


# ---------------------------------------------------------------- 目录端点


async def test_catalog_endpoint_includes_mcp(mcp_port: int) -> None:
    """★ 端到端：/v1/tools 里同时有内置工具与 MCP 工具。"""
    from atlas_server.config import Settings, get_settings
    from atlas_server.main import create_app
    from httpx import ASGITransport

    base = get_settings()
    patched = Settings(
        **{
            **base.model_dump(),
            "mcp_servers": [McpServerConfig(name="local", url=f"http://127.0.0.1:{mcp_port}/mcp")],
        }
    )

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: patched
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.get("/v1/tools")

    assert res.status_code == 200, res.text
    names = {t["name"] for t in res.json()["data"]}
    assert "write_todos" in names, "内置工具不见了"
    assert "mcp:local:add" in names, "MCP 工具没并进目录"
