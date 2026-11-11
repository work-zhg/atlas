"""PLAN-05 环境重建之后

校验（在回放容器里执行，工作区在 /workspace）。"""

import subprocess
import sys


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        fail(f"缺少 {path}")
    return ""


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


if read("value.txt").strip() != "42":
    fail(f"value.txt 应为 42，实际 {read('value.txt').strip()!r}")
if read("report.py") != read("/verifier/report.py"):
    fail("report.py 被改了：应当重建依赖，而不是改代码")
print("ok")
