"""装本机浏览器组件（atlas-connect browser install）。

电脑主人自己装，只有要用浏览器的人才装（Chrome 加中文字体几百 MB）。装的东西和平台的浏览器沙箱
镜像是同一套（docker/browser/install.sh）：

    系统包        虚拟屏幕 Xvfb、画面服务 x11vnc、系统键鼠 xdotool、剪贴板 xclip、中文字体
                  （apt，要 sudo）
    浏览器        已经有 Chrome / Chromium 就用它；没有：x86_64 装 Google Chrome，
                  arm64 装 Debian 的 chromium
    atlas-browserd  守护和 browser 命令（装进连接器自己的 Python 环境）
    Playwright CLI  快照方式的后端（npm 全局装）
    browser 命令    ~/.atlas/bin/browser（连接器拉起 CLI 时加在 PATH 前面）
    两个浏览器技能  ~/.claude/skills/{browser,browser-computer-use}，版本跟着组件走

★ 首版只支持 Linux（包括 Windows 的 WSL）：虚拟屏幕用 Xvfb，macOS 上没有现成的。
★ 不碰电脑主人日常用的 Chrome 的 profile：本机浏览器的 profile 在 ~/.atlas/browser 下。
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .browser import BROWSERS, BrowserEnv

__all__ = ["BrowserInstaller", "InstallFailed"]

PLAYWRIGHT_CLI_VERSION = "0.1.24"
APT_PACKAGES = (
    "xvfb",
    "x11vnc",
    "xdotool",
    "xclip",
    "fonts-noto-cjk",
    "fonts-noto-color-emoji",
    "ca-certificates",
)
CHROME_DEB = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
SKILLS = ("browser", "browser-computer-use")
#: 我们装的技能目录里放这个文件：重装时覆盖自己的，不覆盖电脑主人自己的同名技能
SKILL_MARKER = ".atlas-browserd"


class InstallFailed(Exception):
    pass


class BrowserInstaller:
    def __init__(
        self,
        env: BrowserEnv,
        *,
        browserd: str | None = None,
        system: bool = True,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._env = env
        self._browserd = browserd
        self._system = system
        # 立刻输出：和 apt、pip 的输出按先后排在一起
        self._say = say or (lambda text: print(text, flush=True))

    def install(self) -> None:
        if platform.system() != "Linux":
            raise InstallFailed("首版只支持 Linux（包括 Windows 的 WSL）：macOS 上没有虚拟屏幕")
        # 先装守护：缺轮子时立刻停下，不先跑几分钟 apt
        self._python_package()
        if self._system:
            self._system_packages()
            self._browser()
        self._playwright_cli()
        self._command()
        self._skills()

    # ------------------------------------------------------------------ 系统包、浏览器

    def _system_packages(self) -> None:
        if shutil.which("apt-get") is None:
            packages = " ".join(APT_PACKAGES)
            self._say(f"! 没有 apt-get：请用系统的包管理器装 {packages}，再加 --no-system 重跑")
            return
        self._say("· 装系统包（要 sudo）：" + " ".join(APT_PACKAGES))
        _root(["apt-get", "update"])
        _root(["apt-get", "install", "-y", "--no-install-recommends", *APT_PACKAGES])

    def _browser(self) -> None:
        found = next((shutil.which(b) for b in BROWSERS if shutil.which(b)), None)
        if found:
            self._say(f"· 浏览器：用已有的 {found}")
            return
        if platform.machine() in ("x86_64", "AMD64"):
            self._say("· 装 Google Chrome")
            with tempfile.TemporaryDirectory() as tmp:
                deb = Path(tmp) / "google-chrome.deb"
                urllib.request.urlretrieve(CHROME_DEB, deb)
                os.chmod(tmp, 0o755)
                _root(["apt-get", "install", "-y", str(deb)])
            return
        self._say("· 装 chromium（Debian；Ubuntu 上的 chromium 是 snap，起不来时请自己装 Chrome）")
        _root(["apt-get", "install", "-y", "--no-install-recommends", "chromium"])

    # ------------------------------------------------------------------ 守护、Playwright CLI

    def _python_package(self) -> None:
        """atlas-browserd 装进连接器自己的 Python 环境（browser 命令、守护都用这个 Python 跑）。

        ★ 还没有公开的包源：和连接器一样随轮子发，--browserd 给轮子的路径。
        """
        installed = importlib.util.find_spec("atlas_browserd") is not None
        if self._browserd is None:
            if installed:
                self._say("· atlas-browserd：已装")
                return
            raise InstallFailed(
                "连接器的 Python 环境里没有 atlas-browserd：用 --browserd 给出它的轮子"
                "（atlas_browserd-*.whl，随连接器一起发），如 "
                "atlas-connect browser install --browserd ./atlas_browserd-0.1.0-py3-none-any.whl"
            )
        self._say(f"· 装 atlas-browserd（{self._browserd}）到连接器的 Python 环境")
        _run([sys.executable, "-m", "pip", "install", "--upgrade", self._browserd])
        importlib.invalidate_caches()  # 同一个进程里接着要读它带的技能

    def _playwright_cli(self) -> None:
        if shutil.which("playwright-cli"):
            self._say("· Playwright CLI：已装")
            return
        npm = shutil.which("npm")
        if npm is None:
            self._say(
                "! 没有 npm：快照方式用不了（computer use 不受影响）。装好 Node.js 后执行 "
                f"npm i -g @playwright/cli@{PLAYWRIGHT_CLI_VERSION}"
            )
            return
        self._say(f"· 装 Playwright CLI {PLAYWRIGHT_CLI_VERSION}")
        _run(
            [
                npm,
                "install",
                "--global",
                "--no-audit",
                "--no-fund",
                f"@playwright/cli@{PLAYWRIGHT_CLI_VERSION}",
            ],
            env={**os.environ, "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"},
        )

    # ------------------------------------------------------------------ browser 命令、技能

    def _command(self) -> None:
        """~/.atlas/bin/browser：用连接器的 Python 跑 browser 命令。"""
        bin_dir = self._env.paths.bin
        bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        script = bin_dir / "browser"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m atlas_browserd.command "$@"\n')
        script.chmod(0o755)
        self._say(f"· browser 命令：{script}")

    def _skills(self) -> None:
        """装进 Claude Code 的技能目录。

        平台在会话开始时告诉 CLI 这次能用哪几个（按模型能不能看图）。
        """
        source = importlib.resources.files("atlas_browserd") / "skills"
        target_root = Path.home() / ".claude" / "skills"
        for name in SKILLS:
            target = target_root / name
            if target.exists() and not (target / SKILL_MARKER).exists():
                self._say(f"! {target} 是你自己的同名技能，没有覆盖：浏览器技能 {name} 没装上")
                continue
            target.mkdir(parents=True, exist_ok=True)
            text = (source / name / "SKILL.md").read_text(encoding="utf-8")
            (target / "SKILL.md").write_text(text, encoding="utf-8")
            (target / SKILL_MARKER).write_text("atlas-connect browser install 装的，重装时覆盖\n")
            self._say(f"· 技能 {name}：{target}")


def _root(argv: list[str]) -> None:
    """以 root 执行：已经是 root 就直接跑，否则加 sudo（会在终端里问密码）。"""
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    _run([*prefix, *argv], env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    done = subprocess.run(argv, env=env, check=False)
    if done.returncode != 0:
        raise InstallFailed(f"{' '.join(argv)} 失败（退出码 {done.returncode}）")
