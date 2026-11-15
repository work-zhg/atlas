"""Screens：虚拟屏幕横着分成几块，每个对话一块。

设计：c4-model/agent/browser/design-browser.html 原理 2「屏幕按对话分块」。

    Slot          一块：在屏幕上的位置、它的画面服务（x11vnc 端口、websockify 的 token）
    ScreenTable   哪个对话用哪一块：对话第一次用浏览器或第一次接管时分一块；满了让出最久没用的那块

★ 块按对话（根会话 id）分：子智能体的窗口、接管窗口跟着对话走，人看对话的画面时都看得到。
★ 几个 browser 命令可能同时分配：账本是本地盘上的一个文件，读改写都在文件锁里。
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .paths import VNC_PORT, Paths

__all__ = ["SLOTS", "SLOT_H", "SLOT_W", "ScreenTable", "Slot"]

#: 一块的大小：人看到的画面就是一块
SLOT_W, SLOT_H = 1920, 1080
#: 块数 = 同时各看各的对话数上限（多一块只多一个空闲的截屏进程和 8 MB 屏幕内存）
SLOTS = int(os.environ.get("ATLAS_BROWSER_SLOTS", "6"))


@dataclass(frozen=True)
class Slot:
    index: int

    @property
    def left(self) -> int:
        return self.index * SLOT_W

    @property
    def bounds(self) -> dict[str, int]:
        """窗口铺满这一块（CDP 的窗口位置和大小）。"""
        return {"left": self.left, "top": 0, "width": SLOT_W, "height": SLOT_H}

    @property
    def clip(self) -> str:
        """x11vnc -clip：只截这一块。"""
        return f"{SLOT_W}x{SLOT_H}+{self.left}+0"

    @property
    def vnc_port(self) -> int:
        return VNC_PORT + self.index

    @property
    def token(self) -> str:
        """websockify 按它转到这一块的 x11vnc。"""
        return f"slot{self.index}"

    @classmethod
    def at(cls, left: int, slots: int = SLOTS) -> Slot:
        """窗口左上角落在哪一块（落在屏幕外的算最近的一块）。"""
        return cls(min(max(left // SLOT_W, 0), slots - 1))

    @classmethod
    def all(cls, slots: int = SLOTS) -> list[Slot]:
        return [cls(i) for i in range(slots)]


class ScreenTable:
    def __init__(self, paths: Paths, slots: int = SLOTS) -> None:
        self._paths = paths
        self._slots = slots

    def acquire(self, key: str) -> Slot:
        """这个对话的块：没有就分一块（先用空的，满了让出最久没用的），有就记一次使用。"""
        with self._locked() as table:
            now = time.time()
            if (row := table.get(key)) is None:
                taken = {r["slot"] for r in table.values()}
                free = [i for i in range(self._slots) if i not in taken]
                if free:
                    index = free[0]
                else:
                    oldest = min(table, key=lambda k: table[k]["used"])
                    index = table.pop(oldest)["slot"]
                row = table[key] = {"slot": index}
            row["used"] = now
            return Slot(row["slot"])

    def find(self, key: str) -> Slot | None:
        """只查不分（看画面不为此占一块）。"""
        with self._locked() as table:
            row = table.get(key)
            return Slot(row["slot"]) if row is not None else None

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self._paths.ensure()
        with self._paths.screens_lock.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                table: dict[str, Any] = json.loads(self._paths.screens.read_text())
            except (OSError, ValueError):
                table = {}
            # 块数变小过（重启时改了配置）：超出的记录作废
            table = {k: r for k, r in table.items() if r.get("slot", self._slots) < self._slots}
            yield table
            self._paths.screens.write_text(json.dumps(table))
