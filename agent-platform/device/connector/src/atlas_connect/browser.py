"""本机浏览器（c4-model/agent/connector/code-connector-browser.html）。

平台不能让电脑运行任意程序，只能经控制连接请连接器做几件固定的事：开接管窗口、关接管窗口、
接画面（再加人在画面里复制粘贴用的剪贴板）；模型执行的 browser 命令发现守护没在跑时，经本机套接字
请连接器拉起。每件事之前都先过本机策略。

    BrowserDesk     入口：浏览器通道（开关接管窗口、剪贴板、状态）、画面流、本机的 browser 命令
    BrowserPolicy   本机策略里关于浏览器的几项：装没装组件、允许远程查看画面
    ComponentProbe  上线报告里的浏览器部分：装没装、什么版本、在不在跑、本机允许查看画面吗
    BrowserEnv      守护和 browser 命令共用的环境：守护目录、虚拟屏幕号、调试端口、块数
    DaemonLauncher  一台电脑一个守护：在跑就用，不在就拉起（profile 在电脑主人目录下，长期保留）
    IdleReaper      没有命令、没人看画面、没有接管窗口超过闲置时长，就关掉守护和 Chrome
                    （profile 留着）
    ViewSplicer     一条画面流：问守护这个对话在第几块，接到那一块的画面服务，两边原样转发

★ 画面流只带会话和对话，不带地址和端口：连接器自己找本机画面服务（只听守护目录里的 Unix 套接字），
  平台借画面流连不到电脑上的其他程序。
★ 守护的命令只有固定几条（atlas-browserd screen slot · takeover open / close · clipboard · stop），
  不转发平台给的任意命令。
★ 浏览器由连接器拉起，不挂在 bridge 下面：一台电脑一个浏览器，几个会话共用；bridge 跟着会话走。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import shutil
import signal
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas_devicelink import Channel, ChannelClosed

from .audit import AuditLog
from .config import LocalPolicy, Paths, Rejection

__all__ = [
    "BROWSER_ENV_NAMES",
    "BrowserDesk",
    "BrowserEnv",
    "BrowserPolicy",
    "ComponentProbe",
    "DaemonLauncher",
    "IdleReaper",
]

logger = logging.getLogger(__name__)

#: 浏览器组件要有的命令（缺一个就算没装好）
REQUIRED_TOOLS = ("Xvfb", "x11vnc", "xdotool", "xclip", "playwright-cli")
BROWSERS = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")
#: 拉起守护后等 Chrome 就绪的时限
READY_TIMEOUT_S = 60.0
#: 交给 CLI 的环境变量（bridge 按白名单传给 CLI，browser 命令据此找到守护、自己的窗口和那一块屏幕）
BROWSER_ENV_NAMES = (
    "ATLAS_BROWSER_SESSION",
    "ATLAS_BROWSER_SCREEN",
    "ATLAS_BROWSER_DIR",
    "ATLAS_BROWSER_DISPLAY",
    "ATLAS_BROWSER_CDP_PORT",
    "ATLAS_BROWSER_SLOTS",
    "ATLAS_BROWSER_VNC_UNIX",
    "ATLAS_BROWSER_NO_SANDBOX",
    "ATLAS_BROWSER_CHROMIUM",
    "ATLAS_CONNECT_SOCKET",
)

#: 画面流的关闭码（设备网关原样交给 Agent 服务）
VIEW_REFUSED = 4403
VIEW_NO_SCREEN = 4404
VIEW_LOST = 4502


class BrowserFailed(Exception):
    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message

    @classmethod
    def of(cls, rejection: Rejection) -> BrowserFailed:
        return cls(403, rejection.reason, rejection.message)


# ═══════════════════════════════════ 环境、探测、策略 ═══════════════════════════════════


@dataclass(frozen=True)
class BrowserEnv:
    """守护和 browser 命令共用的环境（同一台电脑上两边必须一致）。"""

    paths: Paths
    policy: LocalPolicy

    @property
    def root(self) -> Path:
        """守护目录：profile、就绪标记、分块账本、画面服务的套接字都在这里（0700）。"""
        return self.paths.browser

    def daemon_env(self) -> dict[str, str]:
        b = self.policy.browser
        env = {
            "ATLAS_BROWSER_DIR": str(self.root),
            "ATLAS_BROWSER_DISPLAY": b.display,
            "ATLAS_BROWSER_CDP_PORT": str(b.cdp_port),
            "ATLAS_BROWSER_SLOTS": str(b.slots),
            # 画面服务只听 Unix 套接字，不开 websockify：画面走控制连接里的画面流
            "ATLAS_BROWSER_VNC_UNIX": "1",
            # 电脑上 Chrome 的沙盒照常开着；以 root 运行时 Chrome 不允许开沙盒
            "ATLAS_BROWSER_NO_SANDBOX": "1" if _is_root() else "0",
        }
        if b.chromium:
            env["ATLAS_BROWSER_CHROMIUM"] = b.chromium
        return env

    def cli_env(self, session: str, screen: str) -> dict[str, str]:
        """拉起 bridge 时加给 CLI 的：会话名、对话（屏幕的哪一块）、守护的位置、连接器的套接字，
        PATH 前面加上 browser 命令所在的目录。"""
        return {
            **self.daemon_env(),
            "ATLAS_BROWSER_SESSION": session,
            "ATLAS_BROWSER_SCREEN": screen,
            "ATLAS_CONNECT_SOCKET": str(self.paths.socket),
            "PATH": f"{self.paths.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        }

    def process_env(self) -> dict[str, str]:
        return {**os.environ, **self.daemon_env()}


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


class ComponentProbe:
    """上线报告里的浏览器部分。平台据此判断这台电脑能不能用浏览器（「我的设备」里也显示）。"""

    def __init__(self, env: BrowserEnv) -> None:
        self._env = env

    def missing(self) -> list[str]:
        if platform.system() != "Linux":
            return ["Linux（首版只支持 Linux，包括 Windows 的 WSL）"]
        out = []
        if importlib.util.find_spec("atlas_browserd") is None:
            out.append("atlas-browserd")
        out.extend(t for t in REQUIRED_TOOLS if shutil.which(t) is None)
        chromium = self._env.policy.browser.chromium
        if not any(shutil.which(b) for b in ((chromium,) if chromium else BROWSERS)):
            out.append("Chrome / Chromium")
        return out

    def report(self) -> dict[str, Any]:
        missing = self.missing()
        return {
            "installed": not missing,
            "version": _browserd_version(),
            "missing": missing,
            "running": daemon_pid(self._env.root) is not None,
            "remote_view": self._env.policy.browser.remote_view,
        }


def _browserd_version() -> str | None:
    try:
        return importlib.metadata.version("atlas-browserd")
    except importlib.metadata.PackageNotFoundError:
        return None


class BrowserPolicy:
    """本机策略里关于浏览器的几项。电脑主人改，平台改不了。"""

    def __init__(self, policy: LocalPolicy, probe: ComponentProbe) -> None:
        self._policy = policy
        self._probe = probe

    def check(self, op: str) -> Rejection | None:
        """op：ensure（模型的 browser 命令）· takeover · view · clipboard · status。"""
        if op == "status":
            return None
        if missing := self._probe.missing():
            return Rejection(
                "browser_not_installed",
                f"这台电脑没装浏览器组件（缺 {'、'.join(missing)}）："
                "在电脑上运行 atlas-connect browser install，装好后重启连接器",
            )
        if op in ("takeover", "view", "clipboard") and not self._policy.browser.remote_view:
            return Rejection(
                "remote_view_off",
                "这台电脑不允许远程查看浏览器画面：电脑主人在本机策略里关掉了它"
                "（~/.atlas/connect.toml 的 [browser] remote_view）",
            )
        return None


# ═══════════════════════════════════ 守护 ═══════════════════════════════════


def daemon_pid(root: Path) -> int | None:
    """常驻的 serve 在跑就返回它的进程号。"""
    try:
        pid = int((root / "serve.pid").read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


class DaemonLauncher:
    """一台电脑一个守护。拉起它的命令固定（python -m atlas_browserd serve），在电脑主人的目录下。"""

    def __init__(self, env: BrowserEnv) -> None:
        self._env = env
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return daemon_pid(self._env.root) is not None

    async def ensure(self) -> bool:
        """在跑就直接用；不在就拉起并等 Chrome 就绪。返回是不是这次拉起的。

        起不来抛 BrowserFailed。
        """
        async with self._lock:
            if await asyncio.to_thread(self._ready):
                return False
            root = self._env.root
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (root / "daemon.log").open("ab") as log:
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "atlas_browserd",
                    "serve",
                    env=self._env.process_env(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    cwd=str(root),
                    # 活得比这次请求长；连接器退出时由 shutdown 关掉
                    start_new_session=True,
                )
            deadline = time.monotonic() + READY_TIMEOUT_S
            while not await asyncio.to_thread(self._ready):
                if time.monotonic() > deadline:
                    raise BrowserFailed(
                        500,
                        "browser_failed",
                        f"浏览器 {int(READY_TIMEOUT_S)} 秒内没有起来"
                        f"（日志在 {root / 'daemon.log'}）",
                    )
                await asyncio.sleep(0.5)
            return True

    async def stop(self) -> bool:
        """关掉守护（连同虚拟屏幕、Chrome、画面服务）。profile 留着，下次拉起还是登录状态。"""
        pid = daemon_pid(self._env.root)
        if pid is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while daemon_pid(self._env.root) == pid and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        with contextlib.suppress(ProcessLookupError):
            if daemon_pid(self._env.root) == pid:
                os.kill(pid, signal.SIGKILL)
        return True

    def _ready(self) -> bool:
        """就绪标记在、而且它记的那次启动的 Chrome 还连得上（同 supervisor.ready）。"""
        try:
            marker = json.loads((self._env.root / "ready.json").read_text())
        except (OSError, ValueError):
            return False
        port = self._env.policy.browser.cdp_port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
                version = json.loads(resp.read())
        except (OSError, ValueError):
            return False
        return bool(version.get("webSocketDebuggerUrl") == marker.get("cdp"))


class DaemonCommand:
    """给守护发固定的几条命令（一次性的子进程，结果是 stdout 最后一行 JSON）。"""

    def __init__(self, env: BrowserEnv) -> None:
        self._env = env

    async def run(self, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "atlas_browserd",
            *args,
            env=self._env.process_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise BrowserFailed(500, "browser_failed", f"浏览器命令 {args[0]} 超时") from None
        lines = [line for line in out.decode(errors="replace").splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else {}
        except ValueError:
            result = {}
        if proc.returncode != 0 or "error" in result:
            detail = result.get("error") or err.decode(errors="replace").strip()[-300:]
            raise BrowserFailed(500, "browser_failed", f"浏览器命令 {args[0]} 失败：{detail}")
        return result

    async def slot_of(self, screen: str) -> int | None:
        slot = (await self.run("screen", "slot", screen)).get("slot")
        return int(slot) if isinstance(slot, int) else None


@dataclass
class IdleReaper:
    """判断闲置：最近一条 browser 命令、有没有画面流连着、有没有没关的接管窗口。"""

    launcher: DaemonLauncher
    idle_s: float
    clock: Any = time.monotonic
    last_used: float = field(default=0.0)
    views: int = 0
    takeovers: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.last_used = self.clock()

    def touch(self) -> None:
        self.last_used = self.clock()

    def idle(self) -> bool:
        return (
            self.views == 0 and not self.takeovers and self.clock() - self.last_used > self.idle_s
        )

    async def tick(self) -> bool:
        """闲置超时就关掉守护。返回关了没有。"""
        if not self.launcher.running or not self.idle():
            return False
        return await self.launcher.stop()


# ═══════════════════════════════════ 入口 ═══════════════════════════════════


class BrowserDesk:
    def __init__(self, policy: LocalPolicy, paths: Paths, audit: AuditLog) -> None:
        self.env = BrowserEnv(paths, policy)
        self.probe = ComponentProbe(self.env)
        self._policy = BrowserPolicy(policy, self.probe)
        self.launcher = DaemonLauncher(self.env)
        self._daemon = DaemonCommand(self.env)
        self.reaper = IdleReaper(self.launcher, policy.browser.idle_stop_s)
        self._audit = audit

    def report(self) -> dict[str, Any]:
        return self.probe.report()

    def cli_env(self, launch: dict[str, Any]) -> dict[str, str]:
        """开会话：智能体开了浏览器（启动参数带 browser）、这台电脑装了组件，就给 CLI 加上浏览器的
        环境。会话名 = 会话 id，对话 = 根会话 id（子智能体的窗口和父会话在同一块屏幕上）。"""
        spec = launch.get("browser")
        if not isinstance(spec, dict) or self.probe.missing():
            return {}
        thread = str(launch.get("thread") or "")
        session = str(spec.get("session") or thread)
        return self.env.cli_env(session, str(spec.get("screen") or session))

    # ------------------------------------------------------------------ 本机的 browser 命令

    async def ensure_for_command(self) -> dict[str, Any]:
        """browser 命令每条都来一次：浏览器没在跑就拉起，记一次使用。"""
        try:
            await self._ensure("ensure")
        except BrowserFailed as exc:
            return {"error": exc.message}
        self.reaper.touch()
        return {"ready": True}

    async def _ensure(self, op: str) -> None:
        if (rejection := self._policy.check(op)) is not None:
            raise BrowserFailed.of(rejection)
        if await self.launcher.ensure():
            self._audit.record("拉起浏览器", str(self.env.root))

    # ------------------------------------------------------------------ 浏览器通道

    async def on_browser(self, channel: Channel) -> None:
        """平台的固定请求。首帧就是答复 {"status": …, "body": …}。"""
        head = channel.head
        op = str(head.get("op") or "")
        try:
            body = await self._answer(op, head)
            await _send(channel, {"status": 200, "body": body})
        except BrowserFailed as exc:
            if exc.status == 403:
                self._audit.record("拒绝浏览器请求", f"{op}（{exc.message}）")
            error = {"kind": exc.kind, "message": exc.message}
            await _send(channel, {"status": exc.status, "body": {"error": error}})
        except ChannelClosed:
            return
        except Exception as exc:  # 守护的输出不对、文件读写失败……：原因带回网页，连接器照常
            logger.warning("浏览器请求 %s 失败", op, exc_info=True)
            error = {"kind": "browser_failed", "message": f"电脑上的浏览器出错了：{exc}"}
            with contextlib.suppress(ChannelClosed):
                await _send(channel, {"status": 500, "body": {"error": error}})
        finally:
            await channel.close()

    async def _answer(self, op: str, head: dict[str, Any]) -> dict[str, Any]:
        screen = str(head.get("screen") or "")
        if op == "status":
            running = self.launcher.running
            slot = await self._daemon.slot_of(screen) if running and screen else None
            return {**self.report(), "running": running, "slot": slot}
        if op == "takeover.open":
            takeover = _takeover_id(head)
            url = str(head.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise BrowserFailed(400, "invalid_url", "接管窗口只能打开 http(s) 地址")
            await self._ensure("takeover")
            args = ["takeover", "open", takeover, url, *(["--screen", screen] if screen else [])]
            opened = await self._daemon.run(*args)
            self.reaper.takeovers.add(takeover)
            self.reaper.touch()
            self._audit.record("开接管窗口", f"{head.get('thread')}  {url}")
            return opened
        if op == "takeover.close":
            takeover = _takeover_id(head)
            self.reaper.takeovers.discard(takeover)
            self.reaper.touch()
            if not self.launcher.running:
                return {"closed": False}
            # 只关窗口：登录就留在 profile 里，不比较、不导出
            closed = await self._daemon.run("takeover", "close", takeover)
            self._audit.record("关接管窗口", str(head.get("thread") or ""))
            return closed
        if op in ("clipboard.set", "clipboard.get"):
            return await self._clipboard(op, head)
        raise BrowserFailed(400, "unknown_op", f"不认识的浏览器请求：{op}")

    async def _clipboard(self, op: str, head: dict[str, Any]) -> dict[str, Any]:
        """人在画面里复制粘贴：文字经文件进出（粘贴的常是密码，不进命令行），用完就删。"""
        if (rejection := self._policy.check("clipboard")) is not None:
            raise BrowserFailed.of(rejection)
        if not self.launcher.running:
            raise BrowserFailed(409, "browser_not_running", "浏览器没在运行")
        self.reaper.touch()
        self.env.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.env.root / f"clip-{uuid.uuid4().hex}.txt"
        try:
            if op == "clipboard.set":
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(str(head.get("text") or ""))
                await self._daemon.run("clipboard", "set", str(path), timeout=15)
                return {}
            await self._daemon.run("clipboard", "get", "--out", str(path), timeout=15)
            return {"text": path.read_text(encoding="utf-8")}
        finally:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ 画面流

    async def on_view(self, channel: Channel) -> None:
        """一条画面流：本机允许、浏览器在跑、这个对话有那一块，才接到那一块的画面服务。"""
        screen = str(channel.head.get("screen") or "")
        try:
            if (rejection := self._policy.check("view")) is not None:
                await channel.close(VIEW_REFUSED, rejection.message[:100])
                self._audit.record("拒绝看画面", rejection.message.splitlines()[0])
                return
            if not self.launcher.running:
                await channel.close(VIEW_NO_SCREEN, "浏览器没在运行")
                return
            try:
                slot = await self._daemon.slot_of(screen) if screen else None
            except BrowserFailed:
                slot = None
            if slot is None:
                await channel.close(VIEW_NO_SCREEN, "这个对话还没用过浏览器")
                return
            await self._splice(channel, slot)
        except ChannelClosed:
            return

    async def _splice(self, channel: Channel, slot: int) -> None:
        """ViewSplicer：两边原样转发，不解析、不记录。连着画面算浏览器在用。"""
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.env.root / f"vnc-{slot}.sock")
            )
        except OSError:
            await channel.close(VIEW_LOST, "画面服务连不上")
            return
        self.reaper.views += 1
        self._audit.record("看画面", f"{channel.head.get('thread')}（第 {slot} 块）")

        async def up() -> None:
            async for message in channel:
                writer.write(message if isinstance(message, bytes) else message.encode())
                await writer.drain()

        async def down() -> None:
            while chunk := await reader.read(64 * 1024):
                await channel.send(chunk)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            self.reaper.views -= 1
            self.reaper.touch()
        if tasks[1] in done and not channel.closed:
            await channel.close(VIEW_LOST, "画面服务断了")

    # ------------------------------------------------------------------ 闲置、退出

    async def tick(self) -> None:
        if await self.reaper.tick():
            self._audit.record("闲置关浏览器", f"{int(self.reaper.idle_s)} 秒没人用")

    async def shutdown(self) -> None:
        """连接器退出：浏览器一起关掉（没人管它的闲置了）。"""
        if await self.launcher.stop():
            self._audit.record("关浏览器", "连接器退出")


def _takeover_id(head: dict[str, Any]) -> str:
    raw = str(head.get("takeover") or "")
    if not raw or not all(ch.isalnum() or ch == "-" for ch in raw):
        raise BrowserFailed(400, "invalid_takeover", "接管号不合法")
    return raw


async def _send(channel: Channel, head: dict[str, Any]) -> None:
    await channel.send(json.dumps(head, ensure_ascii=False))
