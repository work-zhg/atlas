"""PLAN-04 断线后结果未知（命令其实还在跑）

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


runs = len(sh("cat /tmp/.export-runs 2>/dev/null").split())
if runs != 1:
    fail(f"export.sh 一共执行了 {runs} 次：导出只能做一次（原来那次其实还在跑）")
lines = [line for line in read("export.log").splitlines() if line.strip()]
if len(lines) != 1:
    fail(f"export.log 应恰好一行，实际 {len(lines)} 行")
print("ok")
