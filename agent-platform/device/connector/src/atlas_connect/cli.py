"""命令行：atlas-connect。

atlas-connect pair <平台地址> [--code 配对码] [--gateway 网关地址]   配对
atlas-connect run                     启动：连上平台并保持（Ctrl-C 退出，正在跑的会话一起停）
atlas-connect status                  正在跑的会话
atlas-connect stop <会话 id 前缀>     结束会话
atlas-connect log [-n 50]             本机日志：平台在这台电脑上做过的事
atlas-connect doctor                  自检：CLI、适配器、允许的目录、能不能连上平台
atlas-connect config                  本机策略文件在哪、内容
atlas-connect unpair                  删掉这台电脑上的设备令牌
                                      （网页上的设备还在，要吊销请在网页上点）
atlas-connect browser install         装本机浏览器组件（虚拟屏幕、Chrome、browser 命令、浏览器技能）
atlas-connect browser status          本机浏览器装没装、在不在跑
atlas-connect browser stop            关掉本机浏览器（登录还在，下次用时自动拉起）
atlas-connect browser reset           关掉并清掉本机浏览器里的全部登录
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import shutil
import signal
import sys
import time
import urllib.request

from . import __version__
from .audit import AuditLog
from .browser import BrowserDesk, BrowserEnv, DaemonLauncher
from .config import LocalPolicy, Paths
from .control import ControlServer, ask
from .credentials import CredentialStore
from .files import FileDesk
from .link import Connector, Fatal, ReportBuilder, gateway_url, pair
from .session import SessionHost

__all__ = ["main"]

OK, BAD = "✓", "✗"


def _store(paths: Paths) -> CredentialStore:
    return CredentialStore(paths.device)


def cmd_pair(args: argparse.Namespace, paths: Paths) -> int:
    paths.ensure()
    policy = LocalPolicy.load(paths.config)
    code = args.code or input("配对码: ").strip()
    gateway = args.gateway or gateway_url(args.server)
    try:
        cred = pair(gateway, code, ReportBuilder(policy).build())
    except (Fatal, ConnectionError) as exc:
        print(f"{BAD} {exc}", file=sys.stderr)
        return 1
    _store(paths).save(cred)
    print(f"{OK} 已配对：{getpass.getuser()} 的 {cred.name or '这台电脑'}")
    print(f"{OK} 设备令牌已保存（{paths.device}）")
    print("  下一步：atlas-connect run")
    return 0


async def _run(paths: Paths) -> int:
    cred = _store(paths).load()
    if cred is None:
        print(
            f"{BAD} 还没有配对：在网页「我的设备」里添加设备，再运行 atlas-connect pair",
            file=sys.stderr,
        )
        return 1
    paths.ensure()
    policy = LocalPolicy.load(paths.config)
    audit = AuditLog(paths.audit)
    browser = BrowserDesk(policy, paths, audit)
    host = SessionHost(policy, paths, audit, extra_env=browser.cli_env)
    host.bridges.kill_stale()
    desk = FileDesk(policy, audit)
    connector = Connector(
        cred,
        ReportBuilder(policy, browser=browser.report),
        on_session=host.on_session,
        on_file=desk.on_file,
        on_browser=browser.on_browser,
        on_view=browser.on_view,
    )
    control = ControlServer(
        paths.socket, host, connector, device=cred.name or cred.device_uuid, browser=browser
    )
    await control.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def reap() -> None:
        while True:
            await asyncio.sleep(60)
            for thread in await host.bridges.reap_idle():
                audit.record("闲置停止", thread)
            with contextlib.suppress(Exception):
                await browser.tick()

    print(f"{OK} 连接器 {__version__} 启动，设备：{cred.name or cred.device_uuid}")
    run = asyncio.create_task(connector.run())
    reaper = asyncio.create_task(reap())
    waiter = asyncio.create_task(stop.wait())
    code = 0
    try:
        done, _ = await asyncio.wait({run, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if run in done and run.exception() is not None:
            print(f"{BAD} {run.exception()}", file=sys.stderr)
            code = 1
    finally:
        reaper.cancel()
        await connector.stop()
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run
        await host.shutdown()
        with contextlib.suppress(Exception):
            await browser.shutdown()
        await control.close()
        print("连接器已退出，正在跑的会话都已停止")
    return code


def cmd_status(paths: Paths) -> int:
    status = asyncio.run(ask(paths.socket, {"cmd": "status"}))
    if status is None:
        print("连接器没在运行（atlas-connect run）")
        return 1
    print(f"设备    {status['device']}    {status['state']}")
    sessions = status["sessions"]
    print(f"会话    {len(sessions)} 个")
    for s in sessions:
        age = int((time.time() - s["started_at"]) // 60)
        mark = "接着平台" if s["attached"] else "空闲"
        print(f"  {s['thread'][:8]}  {s['cli_type']:<12} {s['project']:<40} {mark}  {age} 分钟")
    return 0


def cmd_stop(args: argparse.Namespace, paths: Paths) -> int:
    result = asyncio.run(ask(paths.socket, {"cmd": "stop", "thread": args.thread}))
    if result is None:
        print("连接器没在运行")
        return 1
    if not result.get("stopped"):
        print(f"{BAD} 没有以 {args.thread} 开头的会话")
        return 1
    for thread in result["stopped"]:
        print(f"{OK} 已结束会话 {thread}")
    return 0


def cmd_log(args: argparse.Namespace, paths: Paths) -> int:
    for line in AuditLog(paths.audit).tail(args.n):
        print(line)
    return 0


def cmd_doctor(paths: Paths) -> int:
    policy = LocalPolicy.load(paths.config)
    ok = True
    print(f"{OK} 连接器 {__version__}")
    node = shutil.which("node")
    print(f"{OK} Node.js（{node}）" if node else f"{BAD} 没有 Node.js：CLI 适配器要用它")
    report = ReportBuilder(policy).build()
    for name, cli in policy.clis.items():
        if name in report["clis"]:
            print(f"{OK} {name}：{report['clis'][name]}（{' '.join(cli.command)}）")
        else:
            ok = False
            print(
                f"{BAD} {name}：没找到 {cli.command[0]}"
                + (f"        修复：{cli.install}" if cli.install else "")
            )
    for raw, real in zip(policy.allowed_roots, policy.roots(), strict=True):
        print(
            f"{OK} 允许的目录：{raw}"
            if real.is_dir()
            else f"{BAD} 允许的目录不存在：{raw}（会话里用不了）"
        )
    browser = BrowserDesk(policy, paths, AuditLog(paths.audit)).report()
    if browser["installed"]:
        state = "在跑" if browser["running"] else "没在跑（用时自动拉起）"
        view = "允许" if browser["remote_view"] else "不允许"
        print(f"{OK} 本机浏览器 {browser['version']}：{state}，远程查看画面{view}")
    else:
        missing = "、".join(browser["missing"])
        print(f"- 本机浏览器没装（可选）：缺 {missing}；atlas-connect browser install")
    cred = _store(paths).load()
    if cred is None:
        print(f"{BAD} 还没有配对")
        return 1
    try:
        with urllib.request.urlopen(cred.gateway + "/healthz", timeout=10):
            print(f"{OK} 能访问平台（{cred.gateway}）")
    except OSError as exc:
        ok = False
        print(f"{BAD} 连不上平台（{cred.gateway}）：{exc}")
    return 0 if ok else 1


def cmd_config(paths: Paths) -> int:
    LocalPolicy.load(paths.config)
    print(f"# {paths.config}")
    print(paths.config.read_text(encoding="utf-8"))
    return 0


def cmd_unpair(paths: Paths) -> int:
    _store(paths).clear()
    print(f"{OK} 已删掉这台电脑上的设备令牌。网页上的设备还在，不用了请在「我的设备」里吊销")
    return 0


def cmd_browser(args: argparse.Namespace, paths: Paths) -> int:
    from .install import BrowserInstaller, InstallFailed

    paths.ensure()
    policy = LocalPolicy.load(paths.config)
    env = BrowserEnv(paths, policy)
    desk = BrowserDesk(policy, paths, AuditLog(paths.audit))
    if args.action == "install":
        try:
            BrowserInstaller(env, browserd=args.browserd, system=not args.no_system).install()
        except InstallFailed as exc:
            print(f"{BAD} {exc}", file=sys.stderr)
            return 1
        report = desk.report()
        if not report["installed"]:
            print(f"{BAD} 还缺：{'、'.join(report['missing'])}", file=sys.stderr)
            return 1
        print(f"{OK} 本机浏览器组件已装好（{report['version']}）")
        print("  重启连接器后生效（atlas-connect run；systemd：systemctl restart atlas-connect）")
        return 0
    if args.action == "status":
        report = desk.report()
        missing = "、".join(report["missing"])
        view = "允许" if report["remote_view"] else "不允许"
        print(f"装好了吗   {'是' if report['installed'] else '否：缺 ' + missing}")
        print(f"版本       {report['version'] or '-'}")
        print(f"在跑吗     {'是' if report['running'] else '否'}")
        print(f"远程查看   {view}（{paths.config} 的 [browser]）")
        print(f"profile    {env.root / 'profile'}")
        return 0
    stopped = asyncio.run(DaemonLauncher(env).stop())
    if args.action == "stop":
        print(f"{OK} 已关掉本机浏览器" if stopped else "本机浏览器没在跑")
        return 0
    # reset：清掉 profile（这台电脑上浏览器里的全部登录）
    if not args.yes:
        answer = input("会清掉这台电脑上本机浏览器里的全部登录，继续？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return 1
    shutil.rmtree(env.root / "profile", ignore_errors=True)
    for name in ("sessions", "screens.json", "takeovers"):
        target = env.root / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    AuditLog(paths.audit).record("清浏览器登录", str(env.root / "profile"))
    print(f"{OK} 已清掉本机浏览器里的全部登录")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas-connect", description="Atlas 连接器")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="用网页上的配对码配对这台电脑")
    p.add_argument("server", nargs="?", default="", help="平台地址，如 https://atlas.techstz.com")
    p.add_argument("--code")
    p.add_argument("--gateway", help="直接给设备网关的地址（开发用）")
    sub.add_parser("run", help="启动")
    sub.add_parser("status", help="正在跑的会话")
    s = sub.add_parser("stop", help="结束会话")
    s.add_argument("thread", help="会话 id（前几位就行）")
    lg = sub.add_parser("log", help="本机日志")
    lg.add_argument("-n", type=int, default=50)
    sub.add_parser("doctor", help="自检")
    sub.add_parser("config", help="本机策略")
    sub.add_parser("unpair", help="删掉这台电脑上的设备令牌")
    br = sub.add_parser("browser", help="本机浏览器").add_subparsers(dest="action", required=True)
    inst = br.add_parser("install", help="装本机浏览器组件")
    inst.add_argument("--browserd", help="atlas-browserd 的轮子（已经装过就不用给）")
    inst.add_argument("--no-system", action="store_true", help="不装系统包（已经自己装好了）")
    br.add_parser("status", help="装没装、在不在跑")
    br.add_parser("stop", help="关掉本机浏览器")
    reset = br.add_parser("reset", help="关掉并清掉全部登录")
    reset.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    paths = Paths.default()
    if args.command == "pair":
        if not (args.server or args.gateway):
            print(f"{BAD} 给出平台地址：atlas-connect pair https://你的平台地址", file=sys.stderr)
            return 2
        return cmd_pair(args, paths)
    if args.command == "run":
        return asyncio.run(_run(paths))
    if args.command == "status":
        return cmd_status(paths)
    if args.command == "stop":
        return cmd_stop(args, paths)
    if args.command == "log":
        return cmd_log(args, paths)
    if args.command == "doctor":
        return cmd_doctor(paths)
    if args.command == "config":
        return cmd_config(paths)
    if args.command == "unpair":
        return cmd_unpair(paths)
    if args.command == "browser":
        return cmd_browser(args, paths)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
