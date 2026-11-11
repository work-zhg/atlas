"""POD-03 文件进出（§2.1）

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
    """text 里要有 label 某次跑完时打印的 MARK-DONE 行（CLI 拿到并写下了真实输出）。"""
    _, tokens = marks(label)
    if not tokens:
        fail(f"marks/{label} 没有跑完过")
    if not any(f"MARK-DONE {label} {t}" in text for t in tokens):
        fail(f"没有找到 {label} 的 MARK-DONE 行（应为 MARK-DONE {label} <令牌>）")


import hashlib
import os

#: 上传时的校验和（生成任务时算好的）
EXPECTED = {'a/b/c/deep.txt': '8fc9048cc5452022723a7b204f5b5b7b4f07e17f971038486655f13b93145102', '数据/说明.txt': 'f03af8dae8cd054d795ff3eefe0cda36e6c8b74d02abbf30a84f72e1efb63c6b', 'img/logo.png': 'b83ba51f5e464385616059aac166e21996f3fa66c212fc7a9a4d7deb994b4e69', 'big.txt': '3560154f4bce654e0d4f03d84d1f860a07d2d18aa3a989c17fff3a6323c84541'}


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


listed = {}
for line in read("sums.txt").splitlines():
    if line.strip():
        digest, _, path = line.partition("  ")
        listed[path.strip()] = digest.strip()
for path, digest in EXPECTED.items():
    if listed.get(path) != digest:
        fail(f"CLI 看到的 {path} 和上传的不一致：{listed.get(path)} ≠ {digest}")
    if not os.path.exists(path) or sha256(path) != digest:
        fail(f"下载回来的 {path} 和上传的不一致")
if not os.path.exists("gen.bin") or os.path.getsize("gen.bin") != 65536:
    fail("gen.bin 不存在或不是 65536 字节")
want = read("gen.sha256").split()[0]
if sha256("gen.bin") != want:
    fail("下载回来的 gen.bin 和 CLI 生成的不一致")
print("ok")
