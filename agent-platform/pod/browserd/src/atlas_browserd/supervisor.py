"""Supervisor：拉起并看着守护的进程（虚拟屏幕先起，再起 Chromium、每块的画面服务、窗口看守）。

    serve()     常驻（atlas-browserd serve）：一个沙箱只跑一个（文件锁）；哪个退出了就按同样的
                参数重启，
                Chromium 连续崩溃时退避；就绪 = CDP 能连上，就绪标记写在本地盘
    ready()     就绪标记在、CDP 能连上
    wait()      等就绪（status --wait、browser 命令）
    start_detached()  browser 命令发现守护不在时就地拉起（新会话组：活得比这条命令长）

★ profile 在本地盘上：Chromium 重启后登录还在。
★ 画面口令从环境变量读完就从环境里删掉，存一份到 0600 的文件里（守护被 browser 命令重新拉起时用）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import CDP_PORT, Paths
from .screens import Slot
from .spec import ProcessSpec

__all__ = ["Supervisor", "cdp_version", "ready", "start_detached", "stop_running", "wait"]

logger = logging.getLogger("atlas_browserd")

VIEW_KEY_ENV = "ATLAS_BROWSER_VIEW_KEY"
#: Chromium 连续崩溃时的退避上限（秒）
_MAX_BACKOFF_S = 30.0


def cdp_version(port: int = CDP_PORT, timeout: float = 1.0) -> dict[str, Any] | None:
    """CDP 的 /json/version；连不上返回 None。"""
    try:
        # ★ 不走代理：电脑上常配着 HTTP_PROXY，本机回环经代理就连不上了
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def ready(paths: Paths) -> dict[str, Any] | None:
    """就绪标记在、而且它记的那次启动的 CDP 还连得上：返回标记；否则 None。"""
    try:
        marker = json.loads(paths.ready.read_text())
    except (OSError, ValueError):
        return None
    version = cdp_version()
    if version is None or version.get("webSocketDebuggerUrl") != marker.get("cdp"):
        return None
    return marker


def wait(paths: Paths, timeout_s: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while True:
        if (marker := ready(paths)) is not None:
            return marker
        if time.monotonic() > deadline:
            return None
        time.sleep(0.3)


def start_detached(paths: Paths) -> None:
    """在新会话组里拉起 serve，输出进日志。已经在跑的话 serve 自己会退出（文件锁）。"""
    paths.ensure()
    with paths.log.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "atlas_browserd", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(paths.root),
        )


def stop_running(paths: Paths, timeout_s: float = 15.0) -> bool:
    """停掉常驻的 serve（它再停掉虚拟屏幕、Chromium、画面服务）。没在跑返回 False。

    ★ profile 不动：下次拉起还是登录状态。
    """
    try:
        pid = int(paths.pid.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        paths.pid.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    return True


def _view_key(paths: Paths) -> str | None:
    """画面口令：环境变量优先（读完就删），否则用上次存下的。"""
    key = os.environ.pop(VIEW_KEY_ENV, "")
    if key:
        paths.view_key.write_text(key)
        os.chmod(paths.view_key, 0o600)
        return key
    try:
        return paths.view_key.read_text().strip() or None
    except OSError:
        return None


@dataclass
class _Proc:
    name: str
    argv: list[str]
    proc: subprocess.Popen[bytes] | None = None
    failures: int = 0
    next_start: float = 0.0
    started_at: float = field(default=0.0)


class Supervisor:
    def __init__(self, spec: ProcessSpec) -> None:
        self._spec = spec
        self._paths = spec.paths
        self._procs: list[_Proc] = []
        self._stopping = False

    def serve(self) -> int:
        """常驻。拿不到文件锁（别的 serve 在跑）就直接退出 0。"""
        self._paths.ensure()
        with self._paths.lock.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"ok": True, "already": True}))
                return 0
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
            self._paths.ready.unlink(missing_ok=True)
            self._paths.pid.write_text(str(os.getpid()))
            self._start_all()
            try:
                self._loop()
            finally:
                self.stop()
                self._paths.pid.unlink(missing_ok=True)
        return 0

    def stop(self) -> None:
        self._stopping = True
        self._paths.ready.unlink(missing_ok=True)
        for p in reversed(self._procs):
            _terminate(p.proc)

    # ------------------------------------------------------------------ 内部

    def _on_signal(self, signum: int, frame: object) -> None:
        self._stopping = True

    def _start_all(self) -> None:
        spec = self._spec
        argvs: list[tuple[str, list[str] | None]] = [
            ("xvfb", spec.xvfb()),
            ("chromium", spec.chromium_args()),
            *[(f"x11vnc-{s.index}", spec.x11vnc(s)) for s in Slot.all(spec.slots)],
            ("websockify", spec.websockify()),
            ("popup-keeper", spec.popup_keeper()),
        ]
        self._procs = [_Proc(name, argv) for name, argv in argvs if argv is not None]
        if spec.websockify() is None:
            logger.warning("没有画面口令：不开画面服务（浏览器照常可用）")
        self._paths.view_tokens.write_text(spec.view_tokens())
        for p in self._procs:
            self._start(p)
            if p.name == "xvfb":
                self._await_display()

    def _start(self, p: _Proc) -> None:
        logger.info("拉起 %s", p.name)
        p.proc = subprocess.Popen(
            p.argv,
            env=self._spec.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if p.name != "chromium" else None,
            stderr=subprocess.DEVNULL if p.name != "chromium" else None,
        )
        p.started_at = time.monotonic()

    def _await_display(self) -> None:
        socket = Path(f"/tmp/.X11-unix/X{self._spec.display.lstrip(':')}")
        deadline = time.monotonic() + 10
        while not socket.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

    def _loop(self) -> None:
        marked: str | None = None
        while not self._stopping:
            now = time.monotonic()
            for p in self._procs:
                if p.proc is not None and p.proc.poll() is None:
                    if now - p.started_at > 60:
                        p.failures = 0  # 稳定跑过一分钟：退避清零
                    continue
                if p.proc is not None:
                    logger.warning("%s 退出了（%s）", p.name, p.proc.returncode)
                    p.proc = None
                    p.failures += 1
                    p.next_start = now + min(2 ** (p.failures - 1), _MAX_BACKOFF_S)
                if now >= p.next_start:
                    self._start(p)
                    if p.name == "xvfb":
                        self._await_display()
            version = cdp_version()
            cdp = version.get("webSocketDebuggerUrl") if version else None
            if cdp and cdp != marked:
                self._paths.ready.write_text(json.dumps({"cdp": cdp, "pid": os.getpid()}))
                marked = cdp
                logger.info("就绪：%s", cdp)
            elif not cdp and marked:
                self._paths.ready.unlink(missing_ok=True)
                marked = None
            time.sleep(1.0)


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
        return
    proc.kill()


def make_spec(paths: Paths) -> ProcessSpec:
    return ProcessSpec(paths=paths, view_key=_view_key(paths))
