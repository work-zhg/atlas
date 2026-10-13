"""测试用的本地 MCP server（streamable_http）。

作为独立进程启动 —— 它自己要跑一个 event loop 与 ASGI 栈，塞进测试进程里
会与 pytest-asyncio 的 loop 打架。

    python -m tests.mcp_server <port>
"""

from __future__ import annotations

import asyncio
import sys

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("atlas-test")


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """原样回显，用于验证参数透传。"""
    return f"echo: {text}"


@mcp.tool()
def read_file(path: str) -> str:
    """与内置文件工具同名 —— 验证模型侧名有命名空间，不会撞车。"""
    return f"remote:{path}"


@mcp.tool()
async def slow(seconds: float) -> str:
    """睡一会儿再返回，用于验证调用超时。"""
    await asyncio.sleep(seconds)
    return "done"


@mcp.tool()
def big(n: int) -> str:
    """返回 n 个字符，用于验证超大结果裁剪。"""
    return "x" * n


@mcp.tool()
def whoami(ctx: Context) -> str:  # type: ignore[type-arg]
    """回显 Atlas 注入的身份头，用于验证它们确实随调用发出。"""
    headers = ctx.request_context.request.headers  # type: ignore[union-attr]
    return f"user={headers.get('x-atlas-user')} run={headers.get('x-atlas-run')}"


@mcp.tool(description="描述超长的工具。" + "填充" * 1200)
def verbose() -> str:
    return "never"


def main() -> None:
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8931
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
