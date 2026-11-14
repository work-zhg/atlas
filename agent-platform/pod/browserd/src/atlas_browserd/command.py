"""BrowserCommand：模型用的 browser 命令（原生智能体的 execute、CLI 智能体的 Bash 里执行）。

    browser open URL · snapshot · click REF · fill REF TEXT · type · press · select · screenshot ·
    tab-list · go-back · reload …   其余子命令原样交给 Playwright CLI（playwright-cli --help）

每次执行：
  1. 确认守护在：不在就就地拉起（用本地盘上的 profile，不导入登录态 —— 登录态只由 Atlas 在沙箱
     建好时导入），等 CDP 就绪
  2. 会话名 = 环境变量 ATLAS_BROWSER_SESSION（平台给每条命令带上，等于会话的 id）：这个会话第一次
     用、或 Chromium 重启过，就让 Playwright CLI 以这个会话名连上 Chromium，再给它开一个自己的窗口
  3. 带会话名转给 Playwright CLI；快照、截图写进会话工作目录的隐藏目录 .atlas/{会话}/browser/，
     输出里给文件路径，模型按需读（能看图的模型直接读截图）

★ 一个会话一个窗口，不是同一个窗口里的一个标签页：后台标签页里 Chromium 停掉 requestAnimationFrame，
  Playwright 点击前的「稳定」检查就一直等到超时（2026-11-14 实测 @playwright/cli 0.1.24：
  两个会话连同一个 Chromium，后台标签页上 click 超时，各自一个窗口就都正常）。
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
import subprocess
import sys
import time
from pathlib import Path

from .paths import CDP_PORT, Paths
from .supervisor import ready, start_detached, wait

__all__ = ["main"]

SESSION_ENV = "ATLAS_BROWSER_SESSION"
#: 守护不在时就地拉起，最多等多久
_START_WAIT_S = 60.0

USAGE = """用法：browser <子命令> [参数]

  browser open <网址>            在你自己的标签页里打开网址
  browser snapshot               拍快照：把页面的无障碍树写进文件，输出里给路径，读它找元素编号
  browser click <编号>            点击（编号如 e8，来自最近一次快照）
  browser fill <编号> <文字>      填写输入框
  browser type <文字> · press <键> · select <编号> <值> · check <编号>
  browser screenshot             截图写进文件（能看图时读它）
  browser go-back · reload

页面一变，旧编号就作废，要重新 snapshot。遇到登录、验证码：请用户接管（request_takeover）。
一个会话只用自己的这一个页面：不要开新标签页（后台标签页里点不动）。
其余子命令见 playwright-cli --help。"""


def _cli() -> str:
    found = shutil.which("playwright-cli")
    if found is None:
        raise SystemExit("browser：浏览器镜像里没有 playwright-cli")
    return found


def _ensure_daemon(paths: Paths) -> dict[str, object]:
    marker = ready(paths)
    if marker is not None:
        return marker
    start_detached(paths)
    marker = wait(paths, _START_WAIT_S)
    if marker is None:
        raise SystemExit(f"browser：浏览器没有起来（日志在 {paths.log}）")
    return marker


def _attach(paths: Paths, cli: str, session: str, marker: dict[str, object], out: Path) -> None:
    """这个会话还没连上这一次启动的 Chromium：连上，再开一个自己的窗口、选中它。"""
    record = paths.sessions / f"{_safe(session)}.json"
    try:
        if json.loads(record.read_text()).get("cdp") == marker.get("cdp"):
            return
    except (OSError, ValueError):
        pass
    config = paths.sessions / f"{_safe(session)}.config.json"
    config.write_text(json.dumps({"outputDir": str(out), "outputMode": "file"}))
    base = [cli, f"-s={session}"]
    subprocess.run([*base, "detach"], capture_output=True, check=False)
    attached = subprocess.run(
        [*base, f"--config={config}", "attach", f"--cdp=http://127.0.0.1:{CDP_PORT}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if attached.returncode != 0:
        raise SystemExit(f"browser：连不上浏览器：{attached.stderr.strip() or attached.stdout}")
    _own_window(base, session)
    record.write_text(json.dumps({"cdp": marker.get("cdp")}))


def _own_window(base: list[str], session: str) -> None:
    """开一个只属于这个会话的窗口（地址带上会话名好认），在 Playwright CLI 里选中它。"""
    from .cdp import connect

    tag = f"about:blank#atlas-session-{session}"

    async def open_window() -> None:
        async with connect() as cdp:
            await cdp.new_window(tag)

    asyncio.run(open_window())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        listed = subprocess.run(
            [*base, "tab-list"], capture_output=True, text=True, check=False
        ).stdout
        for line in listed.splitlines():
            if tag in line and (found := re.match(r"\s*-\s*(\d+):", line)):
                subprocess.run(
                    [*base, "tab-select", found.group(1)], capture_output=True, check=False
                )
                return
        time.sleep(0.2)


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
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    paths = Paths()
    paths.ensure()
    session = _safe(os.environ.get(SESSION_ENV, "") or "default")
    out = Path.cwd() / ".atlas" / session / "browser"
    out.mkdir(parents=True, exist_ok=True)
    cli = _cli()
    marker = _ensure_daemon(paths)
    _attach(paths, cli, session, marker, out)
    if args[0] == "open":
        args[0] = "goto"
    args = _with_output_file(args, out)
    env = {**os.environ, "PLAYWRIGHT_CLI_SESSION": session}
    os.execve(cli, [cli, f"-s={session}", *args], env)
    return 0  # 走不到


if __name__ == "__main__":
    sys.exit(main())
