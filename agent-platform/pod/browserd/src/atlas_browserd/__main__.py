"""PlatformCommands：Atlas 经 execd 执行的命令行（atlas-browserd <子命令>）。

    serve                                  常驻：起虚拟屏幕、Chromium、画面服务并看着
    stop                                   停掉常驻的 serve（电脑上闲置关停；profile 留着）
    status [--wait N]                      CDP 就绪没有（等最多 N 秒）
    state import FILE                      导入登录态（导完删文件）
    state export --sites a,b [--origins …] --out FILE   导出这些站点的登录态
    state forget SITE                      清掉一个站点的 cookie 和本地存储
    takeover open ID URL [--screen KEY]    记下此刻的 cookie，在对话 KEY 那一块屏幕上开接管窗口
    takeover close ID [--out FILE]         关窗口；给了 --out（沙箱）再比较前后、导出登录态
    clipboard set FILE                     把文件里的文字放进虚拟屏幕的剪贴板（放完删文件）
    clipboard get --out FILE               把虚拟屏幕剪贴板里的文字写到文件
    screen slot KEY                        对话 KEY 在屏幕的第几块（没用过浏览器是 null）
    windows keep                           常驻（守护拉起）：网页弹出的窗口挪回弹出它的那一块

一次性的命令把结果写成 stdout 最后一行 JSON；失败时退出码非 0，JSON 里有 error（给人看的话）。
★ 登录态文件都在本地盘（/tmp/atlas-browser），不经过 /data；写出的文件 0600。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .paths import Paths

__all__ = ["main"]


def _emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _write(path: str, data: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


async def _with_cdp_async(work: Callable[[Any], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """经 Playwright 连上（会给所有页面打开 Runtime）：只用于沙箱里登录态的导入导出。"""
    from .cdp import connect

    async with connect() as cdp:
        return await work(cdp)


def _with_cdp(work: Callable[[Any], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    return asyncio.run(_with_cdp_async(work))


def _with_windows(work: Callable[[Any], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """只用浏览器级调试命令（不附着页面、不开 Runtime）。"""
    from .rawcdp import browser_session

    async def run() -> dict[str, Any]:
        async with browser_session() as windows:
            return await work(windows)

    return asyncio.run(run())


def _serve(args: argparse.Namespace, paths: Paths) -> int:
    from .supervisor import Supervisor, make_spec

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths.ensure()
    return Supervisor(make_spec(paths)).serve()


def _stop(args: argparse.Namespace, paths: Paths) -> int:
    from .supervisor import stop_running

    _emit({"stopped": stop_running(paths)})
    return 0


def _status(args: argparse.Namespace, paths: Paths) -> int:
    from .supervisor import ready, wait

    marker = wait(paths, args.wait) if args.wait else ready(paths)
    if marker is None:
        _emit({"ready": False, "error": "浏览器没有就绪（Chromium 没起来，日志在 daemon.log）"})
        return 1
    _emit({"ready": True})
    return 0


def _state(args: argparse.Namespace, paths: Paths) -> int:
    from .state import StateIO

    if args.action == "import":
        path = Path(args.file)
        try:
            bundle = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)  # 登录态不在本地盘上多留
        _emit(_with_cdp(lambda cdp: StateIO(cdp).import_(bundle)))
        return 0
    if args.action == "export":
        sites, origins = _csv(args.sites), _csv(args.origins)
        state = _with_cdp(lambda cdp: StateIO(cdp).export(sites, origins))
        _write(args.out, state)
        _emit({"out": args.out, "cookies": len(state["cookies"])})
        return 0
    _emit(_with_cdp(lambda cdp: StateIO(cdp).forget(args.site)))
    return 0


def _takeover(args: argparse.Namespace, paths: Paths) -> int:
    from .windows import TakeoverWindows

    paths.ensure()
    if args.action == "open":
        _emit(
            _with_windows(lambda w: TakeoverWindows(w, paths).open(args.id, args.url, args.screen))
        )
        return 0
    if not args.out:
        # 电脑上：登录留在 profile 里，只关窗口
        _emit(_with_windows(lambda w: TakeoverWindows(w, paths).close(args.id)))
        return 0
    from .rawcdp import browser_session

    async def close() -> dict[str, Any]:
        async with browser_session() as windows:
            return await _with_cdp_async(
                lambda cdp: TakeoverWindows(windows, paths).close(args.id, state=cdp)
            )

    result = asyncio.run(close())
    _write(args.out, result)
    _emit({"out": args.out, "domains": result["domains"]})
    return 0


def _clipboard(args: argparse.Namespace, paths: Paths) -> int:
    from .clipboard import read_text, write_text

    if args.action == "set":
        path = Path(args.file)
        try:
            text = path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)  # 粘贴的可能是密码：不在本地盘上多留
        write_text(text)
        _emit({"chars": len(text)})
        return 0
    text = read_text()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _emit({"out": args.out, "chars": len(text)})
    return 0


def _screen(args: argparse.Namespace, paths: Paths) -> int:
    from .screens import ScreenTable

    slot = ScreenTable(paths).find(args.key)
    _emit({"slot": slot.index if slot else None, "token": slot.token if slot else None})
    return 0


def _windows(args: argparse.Namespace, paths: Paths) -> int:
    from .supervisor import wait
    from .windows import PopupKeeper

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if wait(paths, 60) is None:  # Chromium 还没起来：退出，守护过一会儿再拉起
        return 1
    _emit(_with_windows(lambda windows: PopupKeeper(windows).run()))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas-browserd", description="Atlas 浏览器守护")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("stop")
    status = sub.add_parser("status")
    status.add_argument("--wait", type=float, default=0)
    state = sub.add_parser("state").add_subparsers(dest="action", required=True)
    imp = state.add_parser("import")
    imp.add_argument("file")
    exp = state.add_parser("export")
    exp.add_argument("--sites", required=True)
    exp.add_argument("--origins", default="")
    exp.add_argument("--out", required=True)
    forget = state.add_parser("forget")
    forget.add_argument("site")
    takeover = sub.add_parser("takeover").add_subparsers(dest="action", required=True)
    opn = takeover.add_parser("open")
    opn.add_argument("id")
    opn.add_argument("url")
    opn.add_argument("--screen")
    cls = takeover.add_parser("close")
    cls.add_argument("id")
    cls.add_argument("--out")
    clipboard = sub.add_parser("clipboard").add_subparsers(dest="action", required=True)
    clipboard.add_parser("set").add_argument("file")
    clipboard.add_parser("get").add_argument("--out", required=True)
    screen = sub.add_parser("screen").add_subparsers(dest="action", required=True)
    screen.add_parser("slot").add_argument("key")
    windows = sub.add_parser("windows").add_subparsers(dest="action", required=True)
    windows.add_parser("keep")
    return parser


_COMMANDS: dict[str, Callable[[argparse.Namespace, Paths], int]] = {
    "serve": _serve,
    "stop": _stop,
    "status": _status,
    "state": _state,
    "takeover": _takeover,
    "clipboard": _clipboard,
    "screen": _screen,
    "windows": _windows,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args, Paths())
    except Exception as exc:  # 一次性命令：原因给 Atlas（再转给人）
        _emit({"error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
