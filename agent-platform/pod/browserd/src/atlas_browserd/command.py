"""BrowserCommand：模型用的 browser 命令（原生智能体的 execute、CLI 智能体的 Bash 里执行）。

两种操作方式，作用在同一个窗口里（c4-model/agent/browser/design-browser-device.html 原理 2）：

    快照          open · snapshot · click REF · fill REF TEXT · type · press · select · screenshot ·
                  tab-list · go-back · reload …   交给 Playwright CLI（playwright-cli --help）
    computer use  look · tap · double-tap · drag · scroll · key · paste · copy-page · go
                  看截图、用系统键鼠（screen.py），完全不经过调试接口

每次执行：
  1. 确认守护在：沙箱里不在就就地拉起（用本地盘上的 profile，不导入登录态 —— 登录态只由 Atlas 在
     沙箱建好时导入）；电脑上请连接器拉起（ATLAS_CONNECT_SOCKET：连接器按本机策略拉起、记一次使用，
     闲置久了由它关掉），等 CDP 就绪
  2. 会话名 = 环境变量 ATLAS_BROWSER_SESSION（平台给每条命令带上，等于会话的 id）：这个会话还没有
     窗口、或 Chromium 重启过、或窗口被关了，就开一个自己的窗口，开在对话（ATLAS_BROWSER_SCREEN =
     根会话 id）那一块屏幕上（screens.py）；之后每条命令看一眼块变了没有（块满了让给新对话、又分回来
     时），变了就把窗口挪过去。开窗口、挪窗口只用浏览器级的调试命令（rawcdp.py）
  3. computer use 的子命令交给 ScreenDesk；快照的子命令先让 Playwright CLI 以这个会话名连上 Chromium
     （这一次启动里第一次用快照时）、选中这个会话的窗口，再带会话名转给它。快照、截图写进会话工作
     目录的隐藏目录 .atlas/{会话}/browser/，输出里给文件路径，模型按需读

★ 一个会话一个窗口，不是同一个窗口里的一个标签页：后台标签页里 Chromium 停掉 requestAnimationFrame，
  Playwright 点击前的「稳定」检查就一直等到超时（2026-11-14 实测 @playwright/cli 0.1.24：
  两个会话连同一个 Chromium，后台标签页上 click 超时，各自一个窗口就都正常）。
★ 快照暂时还是官方 Playwright：它一连上就给浏览器里所有页面开 Runtime，而且一直连着，直到浏览器
  重新拉起 —— 用过快照的浏览器，检测自动化的网站（zhipin）会认出来，computer use 也救不回来。
★ open 换成 goto：会话已经连在常驻的 Chromium 上，open 会另起一个浏览器。
★ snapshot、screenshot 没给 --filename 时补上：单独执行的 snapshot 默认把整棵树打在输出里。
★ 子智能体有自己的会话，也就有自己的窗口，可以和父会话并行操作。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .paths import CDP_PORT, Paths
from .screens import ScreenTable, Slot
from .supervisor import ready, start_detached, wait

__all__ = ["SessionWindow", "main"]

SESSION_ENV = "ATLAS_BROWSER_SESSION"
#: 对话（根会话 id）：子智能体的窗口和父会话在同一块屏幕上。没给就按会话名分
SCREEN_ENV = "ATLAS_BROWSER_SCREEN"
#: 电脑上：连接器的本机套接字。有它就请连接器拉起守护，没有（沙箱里）就自己拉起
CONNECT_SOCKET_ENV = "ATLAS_CONNECT_SOCKET"
#: 守护不在时拉起，最多等多久
_START_WAIT_S = 60.0

USAGE = """用法：browser <子命令> [参数]

快照（读页面清单、按编号点；纯文本模型也能用）：
  browser open <网址>            在你自己的窗口里打开网址
  browser snapshot               拍快照：把页面的无障碍树写进文件，输出里给路径，读它找元素编号
  browser click <编号>            点击（编号如 e8，来自最近一次快照）
  browser fill <编号> <文字>      填写输入框
  browser type <文字> · press <键> · select <编号> <值> · check <编号>
  browser screenshot             截图写进文件（能看图时读它）
  browser go-back · reload
  页面一变，旧编号就作废，要重新 snapshot。其余子命令见 playwright-cli --help。

computer use（看截图、用系统键鼠；要能看图；检测自动化的网站只用这一种）：
  browser look                   截你这一块屏幕（1280×720），输出里给路径，读它
  browser tap <x> <y>            点击截图上的坐标 · double-tap <x> <y> 双击
  browser drag <x1> <y1> <x2> <y2>        拖动（滑块）
  browser scroll <x> <y> <up|down|left|right> [格数]
  browser key <键>               按键：Enter、Tab、Escape、ctrl+a……
  browser paste <文字>           把文字粘贴进当前输入框（中文也行）
  browser copy-page              全选、复制，取整页纯文本
  browser go <网址>              在地址栏里输入网址、回车
  每个动作做完会自动再截一张，输出里给路径：看它确认点中了没有。

下载：
  browser downloads              下载目录里最近的文件：完成、下载中、卡住、等确认、是不是错误页
  browser downloads --wait [秒]  等下载完（默认最多 60 秒）再列。下载目录是固定的，不用自己找

遇到登录、验证码：请用户接管（request_takeover）。
一个会话只用自己的这一个窗口：不要开新标签页（后台标签页里点不动）。"""


def _cli() -> str:
    found = shutil.which("playwright-cli")
    if found is None:
        raise SystemExit("browser：没有 playwright-cli（浏览器组件没装全）")
    return found


def _ensure_daemon(paths: Paths) -> dict[str, Any]:
    marker = ready(paths)
    if marker is not None and not os.environ.get(CONNECT_SOCKET_ENV):
        return marker
    if sock := os.environ.get(CONNECT_SOCKET_ENV):
        # 电脑上：每条命令都告诉连接器一声（它据此判断浏览器在用，不会被当闲置关掉）
        _ask_connector(sock, {"cmd": "browser.ensure"})
    elif marker is None:
        start_detached(paths)
    marker = wait(paths, _START_WAIT_S)
    if marker is None:
        raise SystemExit(f"browser：浏览器没有起来（日志在 {paths.log}）")
    return marker


def _ask_connector(path: str, request: dict[str, Any]) -> dict[str, Any]:
    """经本机套接字问连接器。拉起浏览器可能要几十秒。"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(_START_WAIT_S + 30)
            conn.connect(path)
            conn.sendall(json.dumps(request).encode() + b"\n")
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        raise SystemExit(f"browser：连接器没在运行，浏览器起不来（{exc}）") from exc
    answer = json.loads(raw or b"{}")
    if answer.get("error"):
        raise SystemExit(f"browser：{answer['error']}")
    return answer  # type: ignore[no-any-return]


class SessionWindow:
    """这个会话自己的窗口：开在对话那一块；Chromium 重启过、窗口被关了就重开；块变了就挪过去。

    记录（sessions/{会话}.json）：cdp 这次启动的 Chromium、target 窗口里那个页面、slot 在第几块、
    cli 快照（Playwright CLI）连上的是哪一次启动、cli_target 它选中的页面。
    """

    def __init__(self, paths: Paths, session: str, screen: str, marker: dict[str, Any]) -> None:
        self._paths = paths
        self._session = session
        self._screen = screen
        self._cdp = marker.get("cdp")
        self._record = paths.sessions / f"{_safe(session)}.json"
        try:
            self._known: dict[str, Any] = json.loads(self._record.read_text())
        except (OSError, ValueError):
            self._known = {}
        self.target = ""
        self.slot = Slot(0)

    @property
    def tag(self) -> str:
        """新窗口先打开的地址：带上会话名，快照那边好认出它。"""
        return f"about:blank#atlas-session-{self._session}"

    def ensure(self) -> tuple[str, Slot]:
        self.slot = ScreenTable(self._paths).acquire(self._screen)
        known = self._known if self._known.get("cdp") == self._cdp else {}
        self.target = asyncio.run(self._ensure(known.get("target"), known.get("slot")))
        if self.target != known.get("target"):
            known = {}  # 新开的窗口：快照要重新选中它
        self._known = {**known, "cdp": self._cdp, "target": self.target, "slot": self.slot.index}
        self._save()
        return self.target, self.slot

    async def _ensure(self, target: str | None, slot: int | None) -> str:
        from .rawcdp import browser_session

        async with browser_session() as windows:
            if target and await windows.page(target) is not None:
                if slot != self.slot.index:
                    # 块被让给新对话、又分回来：窗口挪到新的那一块
                    await windows.move_target(target, self.slot.bounds)
                return target
            return await windows.new_window(self.tag, self.slot.bounds)

    def attach_cli(self, cli: str, out: Path) -> None:
        """快照：这一次启动的 Chromium 里还没连上就连上，再选中这个会话的窗口。"""
        base = [cli, f"-s={self._session}"]
        if self._known.get("cli") != self._cdp:
            config = self._paths.sessions / f"{_safe(self._session)}.config.json"
            config.write_text(json.dumps({"outputDir": str(out), "outputMode": "file"}))
            subprocess.run([*base, "detach"], capture_output=True, check=False)
            attached = subprocess.run(
                [*base, f"--config={config}", "attach", f"--cdp=http://127.0.0.1:{CDP_PORT}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if attached.returncode != 0:
                raise SystemExit(
                    f"browser：连不上浏览器：{attached.stderr.strip() or attached.stdout}"
                )
            self._known["cli"] = self._cdp
            self._known.pop("cli_target", None)
        if self._known.get("cli_target") != self.target:
            self._select(base)
            self._known["cli_target"] = self.target
        self._save()

    def _select(self, base: list[str]) -> None:
        """在 Playwright CLI 里选中这个会话的窗口：按窗口此刻的地址认（新窗口的地址带会话名）。"""
        url = asyncio.run(self._url()) or self.tag
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            listed = subprocess.run(
                [*base, "tab-list"], capture_output=True, text=True, check=False
            ).stdout
            found = [
                m.group(1)
                for line in listed.splitlines()
                if url in line and (m := re.match(r"\s*-\s*(\d+):", line))
            ]
            if found:
                if len(found) > 1:
                    print(
                        f"browser：有 {len(found)} 个窗口开着同一个网址，选了第一个；"
                        "快照不对的话先 browser go-back 再 snapshot",
                        file=sys.stderr,
                    )
                subprocess.run([*base, "tab-select", found[0]], capture_output=True, check=False)
                return
            time.sleep(0.2)

    async def _url(self) -> str | None:
        from .rawcdp import browser_session

        async with browser_session() as windows:
            page = await windows.page(self.target)
        return str(page.get("url") or "") if page else None

    def _save(self) -> None:
        self._record.write_text(json.dumps(self._known))


def _with_output_file(args: list[str], out: Path) -> list[str]:
    """snapshot、screenshot 没给 --filename：写进会话的隐藏目录，输出里只给路径。"""
    suffix = {"snapshot": "yml", "screenshot": "png"}.get(args[0])
    if suffix is None or any(a.startswith("--filename") for a in args[1:]):
        return args
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return [*args, f"--filename={out / f'{args[0]}-{stamp}-{os.getpid()}.{suffix}'}"]


def _safe(session: str) -> str:
    return "".join(ch for ch in session if ch.isalnum() or ch in "-_") or "default"


def main(argv: list[str] | None = None) -> int:
    from .screen import COMMANDS, OutOfBlock, ScreenDesk

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args[0] == "downloads":
        # 只看下载目录：不用浏览器在，也不用开窗口
        return _downloads(args[1:])
    paths = Paths()
    paths.ensure()
    session = _safe(os.environ.get(SESSION_ENV, "") or "default")
    screen = os.environ.get(SCREEN_ENV, "") or session
    out = Path.cwd() / ".atlas" / session / "browser"
    out.mkdir(parents=True, exist_ok=True)
    marker = _ensure_daemon(paths)
    window = SessionWindow(paths, session, screen, marker)
    target, slot = window.ensure()
    if args[0] in COMMANDS:
        try:
            print(ScreenDesk(paths, target=target, slot=slot, out=out).run(args))
        except (OutOfBlock, ValueError) as exc:
            print(f"browser：{exc}", file=sys.stderr)
            return 2
        except (RuntimeError, OSError) as exc:
            print(f"browser：{exc}", file=sys.stderr)
            return 1
        return 0
    cli = _cli()
    window.attach_cli(cli, out)
    if args[0] == "open":
        args[0] = "goto"
    args = _with_output_file(args, out)
    env = {**os.environ, "PLAYWRIGHT_CLI_SESSION": session}
    os.execve(cli, [cli, f"-s={session}", *args], env)
    return 0  # 走不到


def _downloads(args: list[str]) -> int:
    from . import downloads

    directory = downloads.DOWNLOAD_DIR
    if not args:
        print(downloads.describe(downloads.scan(directory), directory))
        return 0
    if args[0] != "--wait" or len(args) > 2 or (len(args) == 2 and not args[1].isdigit()):
        print("browser：用法 browser downloads [--wait [秒]]", file=sys.stderr)
        return 2
    seconds = int(args[1]) if len(args) == 2 else 60
    files, waited = downloads.wait(min(seconds, 600), directory)
    still = sum(f.state == "下载中" for f in files)
    head = f"等了 {waited:.0f} 秒，还有 {still} 个在下载" if still else f"等了 {waited:.0f} 秒"
    print(f"{head}。\n{downloads.describe(files, directory)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
