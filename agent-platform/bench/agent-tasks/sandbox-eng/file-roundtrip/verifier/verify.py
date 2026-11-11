"""FILE-02 上传往返（§2.2，端到端用例 6.7 files-roundtrip）

校验（工作区在 /workspace，本目录挂在 /verifier）。"""

import glob
import hashlib
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


for root, _, names in os.walk("/verifier/originals"):
    for name in names:
        src = os.path.join(root, name)
        rel = os.path.relpath(src, "/verifier/originals")
        with open(src, "rb") as f:
            want = f.read()
        try:
            with open(rel, "rb") as f:
                got = f.read()
        except FileNotFoundError:
            fail(f"下载回来的工作区里没有 {rel}")
        if got != want:
            fail(f"{rel} 下载回来和上传的不一致（{len(got)} vs {len(want)} 字节）")
        line = f"{hashlib.sha256(want).hexdigest()}  ./{rel}"
        if line not in read("sums.txt"):
            fail(f"沙箱里读到的 {rel} 和上传的不一致（sums.txt 里没有 {line}）")
print("ok")
