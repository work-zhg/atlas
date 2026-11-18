"""ProcessSpec：守护拉起的进程的参数（虚拟屏幕、Chromium、画面服务、窗口看守）。

屏幕       横着 SLOTS 块、每块 1920×1080（screens.py）：每个对话一块，人看到的就是自己那一块
Chromium   有界面（人接管时要看到并操作同一个浏览器）、不用沙盒（沙箱里没有用户命名空间）、
           profile 在本地盘、CDP 只听 127.0.0.1；第一个窗口在第 0 块
画面服务   每块一个 x11vnc（-clip 只截那一块，只听本机）；沙箱里 websockify 对外开 6080，按 token
           转到那一块的 x11vnc，BasicHTTPAuth 口令 = 画面口令。电脑上（ATLAS_BROWSER_VNC_UNIX=1）
           x11vnc 只听守护目录里的 Unix 套接字（目录 0700：同一台电脑上的别的用户连不上），
           不开 websockify：画面由连接器经控制连接里的画面流转出去
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
#: 浏览器能找的名字：沙箱镜像里是 Debian 的 chromium，电脑上常装 Google Chrome
BROWSERS = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")


def chromium_binary() -> str:
    for name in (os.environ.get("ATLAS_BROWSER_CHROMIUM", ""), *BROWSERS):
        if name and (found := shutil.which(name)):
            return found
    raise FileNotFoundError(f"找不到浏览器（ATLAS_BROWSER_CHROMIUM、{'、'.join(BROWSERS)}）")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip() not in ("", "0", "false", "no")


@dataclass(frozen=True)
class ProcessSpec:
    paths: Paths
    view_key: str | None
    display: str = DISPLAY
    slots: int = SLOTS
    cdp_port: int = CDP_PORT
    ws_port: int = VIEW_PORT
    chromium: str = field(default_factory=chromium_binary)
    #: 沙箱里没有用户命名空间，Chromium 的沙盒起不来（隔离靠沙箱本身）；电脑上由连接器关掉它
    #: （以 root 运行时 Chromium 不允许开沙盒，连接器照旧给 1）
    no_sandbox: bool = field(default_factory=lambda: _flag("ATLAS_BROWSER_NO_SANDBOX", True))
    #: 画面服务只听 Unix 套接字（电脑上）
    vnc_unix: bool = field(default_factory=lambda: _flag("ATLAS_BROWSER_VNC_UNIX", False))

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
            *(["--no-sandbox"] if self.no_sandbox else []),
            # Pod 的 /dev/shm 只有 64 MB
            "--disable-dev-shm-usage",
            # 没有 GPU 时 Chromium 不再自动退回 SwiftShader，网页拿不到 WebGL → Cloudflare 的
            # 「Verify you are human」人勾了也判机器人、一直弹回。有 GPU 的电脑上不起作用
            "--enable-unsafe-swiftshader",
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
            *self._vnc_listen(slot),
            "-forever",
            "-shared",
            "-nopw",
            "-quiet",
            "-noxdamage",
        ]

    def _vnc_listen(self, slot: Slot) -> list[str]:
        if self.vnc_unix:
            return ["-rfbport", "0", "-unixsock", str(self.paths.vnc_socket(slot.index))]
        return ["-rfbport", str(slot.vnc_port), "-localhost"]

    def view_tokens(self) -> str:
        """websockify 的 token 表（TokenFile 的格式）。"""
        return "".join(f"{s.token}: 127.0.0.1:{s.vnc_port}\n" for s in Slot.all(self.slots))

    def websockify(self) -> list[str] | None:
        """没有画面口令就不开画面（不能开一个谁都能连的口子）。电脑上不开（画面走连接器）。"""
        if not self.view_key or self.vnc_unix:
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
