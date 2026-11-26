"""对着真网关跑的测试共用的夹具：Docker 不可用时整组跳过（单元测试 test_rules.py 不受影响）。"""

from __future__ import annotations

import os

import pytest
from gw_harness import Gateway, SigningKey, docker_ok


@pytest.fixture(scope="session")
def need_docker() -> None:
    reason = docker_ok()
    if reason:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def gw(tmp_path_factory: pytest.TempPathFactory, need_docker: None):
    """公钥已就绪的网关（kid = k-1）。ATLAS_GATEWAY_TEST_KEEP=1 时测完不删容器，方便看日志。"""
    g = Gateway(tmp_path_factory.mktemp("gw"))
    g.prepare()
    g.key = SigningKey("k-1")  # type: ignore[attr-defined]
    g.set_jwks(g.key)  # type: ignore[attr-defined]
    g.start()
    try:
        g.wait_ready()
        yield g
    finally:
        if os.environ.get("ATLAS_GATEWAY_TEST_KEEP"):
            print(f"\n保留容器 {g.name}")
        else:
            g.stop()
