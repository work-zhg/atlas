"""EXE-12 同一步并行命令（§2.1）

校验（工作区在 /workspace，本目录挂在 /verifier）。"""

import glob
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


text = read("done.txt")
for label in ("p1", "p2", "p3"):
    done_line(text, label)
# 三条并发执行：最早开始到最晚结束不超过 25 秒（串行要 30 秒以上）
times = []
for label in ("p1", "p2", "p3"):
    for kind in ("start", "end"):
        for p in glob.glob(f"marks/{label}/{kind}-*"):
            at = [x for x in read(p).split() if x.startswith("at=")]
            if at:
                times.append(int(at[0][3:]))
if times and max(times) - min(times) > 25:
    fail(f"三条命令没有并行执行（从开始到结束 {max(times) - min(times)} 秒）")
print("ok")
