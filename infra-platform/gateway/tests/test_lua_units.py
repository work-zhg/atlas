"""Lua 模块的单元测试：在网关镜像里用 resty 跑 tests/lua/*_test.lua（最后一行打印 ok 才算过）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from gw_harness import IMAGE

LUA_SRC = Path(__file__).resolve().parents[1] / "src" / "atlas_gateway" / "lua"
TESTS = sorted((Path(__file__).resolve().parent / "lua").glob("*_test.lua"))


@pytest.mark.parametrize("script", TESTS, ids=[p.stem for p in TESTS])
def test_lua(script: Path, need_docker: None) -> None:
    out = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{LUA_SRC}:/lua:ro", "-v", f"{script.parent}:/t:ro",
            IMAGE, "resty", "-I", "/lua", f"/t/{script.name}",
        ],
        capture_output=True, text=True,
    )  # fmt: skip
    assert out.stdout.strip().splitlines()[-1:] == ["ok"], out.stdout + out.stderr
