"""静态检查依赖方向（看 import 语句，不运行代码）：

atlas_server   不得 import langchain* / langgraph* / langsmith
atlas_engine   不得 import mcp / fastmcp / httpx* / atlas_server
               （engine 不认识 MCP，也不反向依赖 server）
atlas_engine/contracts.py   只用标准库：server 照着它交东西，不该因此碰到任何框架类型
其余模块（网关、Higress、用户中心……）不得 import atlas_server / atlas_engine

.venv/bin/python demo/mcp-gateway/check_layers.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANGCHAIN = ("langchain", "langchain_core", "langgraph", "langsmith")
MCP_AND_HTTP = ("mcp", "fastmcp", "httpx", "httpx2", "starlette", "uvicorn")
ATLAS = ("atlas_server", "atlas_engine")

RULES: list[tuple[str, list[Path], tuple[str, ...]]] = [
    (
        "atlas_server 不依赖 langchain / langgraph",
        sorted((HERE / "atlas_server").glob("*.py")),
        LANGCHAIN,
    ),
    (
        "atlas_engine 不认识 MCP、不依赖 server",
        sorted((HERE / "atlas_engine").glob("*.py")),
        (*MCP_AND_HTTP, "atlas_server"),
    ),
    (
        "atlas_engine/contracts.py 只用标准库",
        [HERE / "atlas_engine" / "contracts.py"],
        (*LANGCHAIN, "pydantic", *MCP_AND_HTTP),
    ),
    (
        "网关、Higress 等外围模块不依赖 Atlas",
        [p for p in sorted(HERE.glob("*.py")) if p.name not in ("main.py", "check_layers.py")],
        ATLAS,
    ),
]


def imports(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def main() -> int:
    broken = 0
    for title, files, forbidden in RULES:
        bad = [
            f"{p.relative_to(HERE)}:{line} import {name}"
            for p in files
            for line, name in imports(p)
            if name.split(".")[0] in forbidden
        ]
        print(f"{'✗' if bad else '✓'} {title}（{len(files)} 个文件）")
        for b in bad:
            print(f"    {b}")
        broken += bool(bad)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
