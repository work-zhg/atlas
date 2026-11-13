"""本机日志（AuditLog）：平台在这台电脑上做过的事。

开会话、拒绝、结束、每次远程读文件都记一行；电脑主人用 atlas-connect log 查看。
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

__all__ = ["AuditLog"]


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, kind: str, detail: str) -> None:
        line = f"{time.strftime('%m-%d %H:%M:%S')}  {kind:<10}  {detail}\n"
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    def tail(self, n: int = 50) -> list[str]:
        if not self._path.exists():
            return []
        with self._path.open(encoding="utf-8") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
