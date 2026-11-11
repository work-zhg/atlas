"""SBF-01 命令执行中沙箱被删（§2.6，端到端用例 6.13 chaos-sandbox-killed 前半）

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


lines = [line for line in read("summary.txt").splitlines() if line.strip()]
if len(lines) != 1:
    fail(f"summary.txt 应恰好一行，实际 {len(lines)} 行（第三步重复做了或没做）")
for name in ("step1.done", "step2.done"):
    if not os.path.exists(name):
        fail(f"缺少 {name}：流水线没有补完")
_, tokens = marks("step2")
if not tokens:
    fail("第二步从没跑完过")
print("ok")
