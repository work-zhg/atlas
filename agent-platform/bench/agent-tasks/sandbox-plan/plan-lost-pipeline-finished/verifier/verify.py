"""PLAN-03 命令中途沙箱没了（其实三步都做完了，只是结果丢了）

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


lines = [line for line in read("summary.txt").splitlines() if line.strip()]
if lines != ["report DATA"]:
    fail(f"summary.txt 应恰好一行 report DATA，实际 {lines}")
runs = len(sh("cat /tmp/.steps-runs 2>/dev/null").split())
if runs != 0:
    fail(f"steps.sh 执行了 {runs} 次（应为 0 次：其实三步都做完了，只是结果丢了）")
print("ok")
