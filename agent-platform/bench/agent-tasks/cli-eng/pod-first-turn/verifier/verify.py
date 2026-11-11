"""POD-01 新会话第一轮（§2.1）

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


import json

EXPECTED = {"华东": (3, 265.74), "华北": (3, 164.75), "华南": (2, 320.10), "西南": (2, 185.35)}
try:
    got = json.loads(read("out.json"))
except ValueError as exc:
    fail(f"out.json 不是 JSON：{exc}")
if set(got) != set(EXPECTED):
    fail(f"地区不对：{sorted(got)}")
for region, (count, total) in EXPECTED.items():
    row = got[region]
    if int(row.get("count", -1)) != count:
        fail(f"{region} 的订单数应为 {count}，是 {row.get('count')}")
    if abs(float(row.get("total", -1)) - total) > 0.011:
        fail(f"{region} 的金额合计应为 {total}，是 {row.get('total')}")
starts, _ = marks("first")
if not starts:
    fail("没有运行 sh marks/mark.sh first 0")
print("ok")
