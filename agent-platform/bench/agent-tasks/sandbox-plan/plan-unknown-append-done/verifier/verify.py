"""PLAN-01 结果未知的追加（其实已经追加了）

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


lines = read("orders.csv").splitlines()
n = lines.count("R-1001,refund,-50")
if n != 1:
    fail(f"退款记录出现了 {n} 次（应恰好一次）")
if lines[:3] != ["id,type,amount", "O-1,order,100", "O-2,order,80"]:
    fail("原有的行被改动了")
print("ok")
