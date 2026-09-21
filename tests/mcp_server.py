"""测试用的本地 MCP server（streamable_http）。

作为独立进程启动 —— 它自己要跑一个 event loop 与 ASGI 栈，塞进测试进程里
会与 pytest-asyncio 的 loop 打架。

    python -m tests.mcp_server <port>
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("atlas-test")


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """原样回显，用于验证参数透传。"""
    return f"echo: {text}"


def main() -> None:
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8931
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
