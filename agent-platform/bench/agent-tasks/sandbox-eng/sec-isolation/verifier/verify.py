"""SEC-03 同一个人的两个会话互相隔离（§2.8，工具测试用例 ISO-02）

校验（工作区在 /workspace，本目录挂在 /verifier）。"""

import glob
import os
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


def marks(label: str) -> tuple[int, list[str]]:
    """计数脚本留下的现场 → (执行次数, 跑完的那几次的令牌)。"""
    starts = glob.glob(f"marks/{label}/start-*")
    tokens = []
    for p in glob.glob(f"marks/{label}/end-*"):
        for part in read(p).split():
            if part.startswith("token="):
                tokens.append(part[len("token="):])
    return len(starts), tokens


def done_line(text: str, label: str) -> None:
    """text 里要有 label 某次跑完时打印的 MARK-DONE 行（模型拿到并写下了真实输出）。"""
    _, tokens = marks(label)
    if not tokens:
        fail(f"marks/{label} 没有跑完过")
    if not any(f"MARK-DONE {label} {t}" in text for t in tokens):
        fail(f"没有找到 {label} 的 MARK-DONE 行（应为 MARK-DONE {label} <令牌>）")


text = read("found.txt")
for leaked in ("topsecret-A", "tmpsecret-A"):
    if leaked in text:
        fail(f"另一个会话的 {leaked} 被看到了")
if "no-sleep" not in text:
    fail("看到了另一个会话的后台进程 sleep 900")
if os.path.exists("secret.txt"):
    fail("另一个会话的 /workspace/secret.txt 出现在这个会话的工作区里")
print("ok")
