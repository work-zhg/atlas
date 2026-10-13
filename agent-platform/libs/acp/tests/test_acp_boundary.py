"""atlas_acp 的包边界与真实 CLI 的能力位形状。"""

from __future__ import annotations

import subprocess
import sys

from atlas_acp.v1 import AgentCaps

#: 装进 Pod 就等于每个会话多背一层的东西
FORBIDDEN = (
    "atlas_server",
    "atlas_engine",
    "atlas_host",
    "atlas_bridge",
    "sqlalchemy",
    "fastapi",
    "redis",
    "langchain",
    "langchain_core",
    "langgraph",
    "boto3",
)


def test_acp_package_is_a_leaf() -> None:
    """★ import atlas_acp 不得拖进本仓其它包或任何重依赖。

    bridge 依赖它并被打进 Pod 镜像 —— 多一个依赖就是每个会话 Pod 多一层字节与一个 CVE 面。
    import-linter 有同名契约（静态），这里在运行时再兜一层：子进程干净 import，查 sys.modules。
    """
    code = (
        "import sys, atlas_acp, atlas_acp.v1;"
        f"bad=[m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "", f"atlas_acp 泄漏了重依赖：{proc.stdout.strip()}"


def test_capabilities_parse_the_shape_the_real_cli_sends() -> None:
    """★ 回归钉：claude-code-acp 0.16.2 实际发的就是这一份（2026-09-30 真机抓取）。

    字段名是 ``sessionCapabilities``（不是 ``session``），resume 用**键存在**表示支持
    （值是空对象，不是 true）。读错的后果不报错：每一轮都当作不能恢复、从头开始，
    而事件流看上去一切正常 —— 只有连着问两轮才会发现它不记得上一轮。
    """
    real = {
        "promptCapabilities": {"image": True, "embeddedContext": True},
        "mcpCapabilities": {"http": True, "sse": True},
        "loadSession": True,
        "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
    }
    caps = AgentCaps.from_raw(real)
    assert caps.load_session and caps.resume and caps.list_sessions
    assert caps.mcp_http and caps.mcp_sse
    assert not caps.close  # 真实 CLI 没有声明 close


def test_a_cli_without_resume_is_not_treated_as_resumable() -> None:
    """没有 resume 键 = 不支持。「以为在继续、实际从零开始」是最坏的失败形态，宁可保守。"""
    assert not AgentCaps.from_raw({"sessionCapabilities": {"fork": {}, "list": {}}}).resume
    assert not AgentCaps.from_raw({}).resume
