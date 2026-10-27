"""按顺序给链路上的每一跳编号打印：① 用户 → ② LLM → … 。

Server、MCP 服务、LLM mock 跑在后台线程里，但它们都在处理请求的过程中打印、Agent 等它们返回，
所以输出顺序就是链路的真实顺序。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count

_n = count(1)
_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"
_muted = False


@contextmanager
def quiet() -> Iterator[None]:
    """这一段不打印（例如启动时拉工具目录）。"""
    global _muted
    _muted = True
    try:
        yield
    finally:
        _muted = False


def hop(actor: str, message: str) -> None:
    if _muted:
        return
    n = next(_n)
    mark = _MARKS[n - 1] if n <= len(_MARKS) else f"({n})"
    print(f"{mark} [{actor}] {message}")


def stage(title: str) -> None:
    print(f"\n── {title}")


def detail(message: str) -> None:
    for line in message.splitlines():
        print(f"      {line}")
