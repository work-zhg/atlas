"""PLAN-02 提交时断开（其实没有执行）

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


if read("version.txt").strip() != "1.4.1":
    fail(f"version.txt 应为 1.4.1，实际 {read('version.txt').strip()}")
subjects = sh("git log --format=%s").splitlines()
if subjects.count("release 1.4.1") != 1 or any(s.startswith("release 1.4.2") for s in subjects):
    fail(f"git 提交记录不对：{subjects}")
print("ok")
