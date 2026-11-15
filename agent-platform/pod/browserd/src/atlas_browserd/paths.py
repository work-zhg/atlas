"""守护在沙箱本地盘上的目录和文件（/tmp 是 emptyDir：随沙箱存活，不是 /data）。

★ profile 不能放 /data：Chromium 的 profile 是 SQLite，放不了对象存储挂载；/data 还是这个人所有
  智能体的 Shell 都能读的地方。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "CDP_PORT",
    "DISPLAY",
    "ROOT",
    "VIEW_PORT",
    "VNC_PORT",
    "Paths",
]

#: 调试接口只开在 127.0.0.1：它能读所有 cookie、执行任意脚本
CDP_PORT = int(os.environ.get("ATLAS_BROWSER_CDP_PORT", "9222"))
#: VNC 只听本机，对外只开带口令的画面端口
VNC_PORT = 5900
VIEW_PORT = int(os.environ.get("ATLAS_BROWSER_VIEW_PORT", "6080"))
DISPLAY = os.environ.get("ATLAS_BROWSER_DISPLAY", ":99")
ROOT = Path(os.environ.get("ATLAS_BROWSER_DIR", "/tmp/atlas-browser"))


class Paths:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    @property
    def profile(self) -> Path:
        return self.root / "profile"

    @property
    def ready(self) -> Path:
        """就绪标记：CDP 的 WebSocket 地址、守护的 pid。Chromium 重启后地址会变。"""
        return self.root / "ready.json"

    @property
    def view_key(self) -> Path:
        """画面口令（0600）。守护自己重启（被 browser 命令拉起）时从这里读。"""
        return self.root / "view.key"

    @property
    def lock(self) -> Path:
        """一个沙箱只跑一个守护。"""
        return self.root / "serve.lock"

    @property
    def log(self) -> Path:
        return self.root / "daemon.log"

    @property
    def takeovers(self) -> Path:
        return self.root / "takeovers"

    @property
    def sessions(self) -> Path:
        """browser 命令记下每个会话名连的是哪一次启动的 Chromium（重启后要重新连）。"""
        return self.root / "sessions"

    @property
    def screens(self) -> Path:
        """哪个对话用屏幕的哪一块（screens.py）。"""
        return self.root / "screens.json"

    @property
    def screens_lock(self) -> Path:
        return self.root / "screens.lock"

    @property
    def view_tokens(self) -> Path:
        """websockify 的 token 表：slotN → 第 N 块的 x11vnc。"""
        return self.root / "view-tokens"

    def ensure(self) -> None:
        for path in (self.root, self.takeovers, self.sessions):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
