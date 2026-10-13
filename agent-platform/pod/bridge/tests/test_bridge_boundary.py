"""atlas_bridge 的包边界：它跑在 Pod 里，不得认识 server。"""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = (
    "atlas_server",
    "atlas_engine",
    "atlas_cluster",
    "sqlalchemy",
    "fastapi",
    "redis",
    "langchain",
    "langgraph",
    "boto3",
)


def test_bridge_package_is_a_leaf() -> None:
    """★ bridge 与 server 的唯一接口是上游协议（Bridge 设计 §3.3）。

    代码级共享会让「升级 server 必须同步升级全部在跑的 Pod」，而 Pod 在跑时没法原地升级。
    import-linter 有同名契约（静态），这里在运行时再兜一层：导入入口所需的全部模块。
    """
    code = (
        "import sys, atlas_bridge.app, atlas_bridge.__main__;"
        f"bad=[m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "", f"atlas_bridge 泄漏了不该有的依赖：{proc.stdout.strip()}"
