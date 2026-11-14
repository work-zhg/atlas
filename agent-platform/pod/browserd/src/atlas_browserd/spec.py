"""ProcessSpec：三个进程的参数（虚拟屏幕、Chromium、画面服务）。

屏幕       1920×1080：接管的人看到的就是这块屏幕
Chromium   有界面（人接管时要看到并操作同一个浏览器）、不用沙盒（strict 档没有用户命名空间）、
           profile 在本地盘、CDP 只听 127.0.0.1
画面服务   x11vnc 只听本机；websockify 对外开 6080，BasicHTTPAuth 口令 = 画面口令
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .paths import CDP_PORT, DISPLAY, VIEW_PORT, VNC_PORT, Paths

__all__ = ["ProcessSpec", "chromium_binary"]

#: 画面服务 BasicHTTPAuth 的用户名（Atlas 那边是同一个）
VIEW_USER = "atlas"


def chromium_binary() -> str:
    for name in (os.environ.get("ATLAS_BROWSER_CHROMIUM", ""), "chromium", "chromium-browser"):
        if name and (found := shutil.which(name)):
            return found
    raise FileNotFoundError("找不到 Chromium（ATLAS_BROWSER_CHROMIUM、chromium、chromium-browser）")


@dataclass(frozen=True)
class ProcessSpec:
    paths: Paths
    view_key: str | None
    display: str = DISPLAY
    screen: str = "1920x1080x24"
    cdp_port: int = CDP_PORT
    vnc_port: int = VNC_PORT
    ws_port: int = VIEW_PORT
    chromium: str = field(default_factory=chromium_binary)

    @property
    def width_height(self) -> tuple[int, int]:
        w, h, _ = self.screen.split("x")
        return int(w), int(h)

    def xvfb(self) -> list[str]:
        return ["Xvfb", self.display, "-screen", "0", self.screen, "-nolisten", "tcp", "-ac"]

    def chromium_args(self) -> list[str]:
        w, h = self.width_height
        return [
            self.chromium,
            f"--user-data-dir={self.paths.profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.cdp_port}",
            # strict 档没有用户命名空间，Chromium 的沙盒起不来；隔离靠沙箱本身
            "--no-sandbox",
            # Pod 的 /dev/shm 只有 64 MB
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--password-store=basic",
            "--lang=zh-CN",
            f"--window-size={w},{h}",
            "--window-position=0,0",
            "about:blank",
        ]

    def x11vnc(self) -> list[str]:
        return [
            "x11vnc",
            "-display",
            self.display,
            "-rfbport",
            str(self.vnc_port),
            "-localhost",
            "-forever",
            "-shared",
            "-nopw",
            "-quiet",
            "-noxdamage",
        ]

    def websockify(self) -> list[str] | None:
        """没有画面口令就不开画面（不能开一个谁都能连的口子）。"""
        if not self.view_key:
            return None
        return [
            "websockify",
            "--auth-plugin",
            "BasicHTTPAuth",
            "--auth-source",
            f"{VIEW_USER}:{self.view_key}",
            f"0.0.0.0:{self.ws_port}",
            f"127.0.0.1:{self.vnc_port}",
        ]

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "ATLAS_BROWSER_VIEW_KEY"}
        env["DISPLAY"] = self.display
        return env
