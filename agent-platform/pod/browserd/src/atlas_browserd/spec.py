"""ProcessSpec：守护拉起的进程的参数（虚拟屏幕、Chromium、画面服务、窗口看守）。

屏幕       横着 SLOTS 块、每块 1920×1080（screens.py）：每个对话一块，人看到的就是自己那一块
Chromium   有界面（人接管时要看到并操作同一个浏览器）、不用沙盒（沙箱里没有用户命名空间）、
           profile 在本地盘、CDP 只听 127.0.0.1；第一个窗口在第 0 块
画面服务   每块一个 x11vnc（-clip 只截那一块，只听本机）；websockify 对外开 6080，按 token 转到
           那一块的 x11vnc，BasicHTTPAuth 口令 = 画面口令
窗口看守   网页弹出的窗口挪回弹出它的那一块（windows.py 的 PopupKeeper）
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field

from .paths import CDP_PORT, DISPLAY, VIEW_PORT, Paths
from .screens import SLOT_H, SLOT_W, SLOTS, Slot

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
    slots: int = SLOTS
    cdp_port: int = CDP_PORT
    ws_port: int = VIEW_PORT
    chromium: str = field(default_factory=chromium_binary)

    @property
    def screen(self) -> str:
        return f"{SLOT_W * self.slots}x{SLOT_H}x24"

    def xvfb(self) -> list[str]:
        return ["Xvfb", self.display, "-screen", "0", self.screen, "-nolisten", "tcp", "-ac"]

    def chromium_args(self) -> list[str]:
        w, h = SLOT_W, SLOT_H
        return [
            self.chromium,
            f"--user-data-dir={self.paths.profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.cdp_port}",
            # 沙箱里没有用户命名空间，Chromium 的沙盒起不来；隔离靠沙箱本身
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

    def x11vnc(self, slot: Slot) -> list[str]:
        """一块一个：-clip 只截这一块，键鼠也落在这一块里（没人连着时不截屏，空闲几乎不占 CPU）。"""
        return [
            "x11vnc",
            "-display",
            self.display,
            "-clip",
            slot.clip,
            "-rfbport",
            str(slot.vnc_port),
            "-localhost",
            "-forever",
            "-shared",
            "-nopw",
            "-quiet",
            "-noxdamage",
        ]

    def view_tokens(self) -> str:
        """websockify 的 token 表（TokenFile 的格式）。"""
        return "".join(f"{s.token}: 127.0.0.1:{s.vnc_port}\n" for s in Slot.all(self.slots))

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
            "--token-plugin",
            "TokenFile",
            "--token-source",
            str(self.paths.view_tokens),
            f"0.0.0.0:{self.ws_port}",
        ]

    def popup_keeper(self) -> list[str]:
        return [sys.executable, "-m", "atlas_browserd", "windows", "keep"]

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "ATLAS_BROWSER_VIEW_KEY"}
        env["DISPLAY"] = self.display
        return env
