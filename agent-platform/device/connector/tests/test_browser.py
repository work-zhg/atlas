"""本机浏览器：本机策略、上线报告、交给 CLI 的环境、闲置关停、浏览器请求、画面流。

不起真的浏览器：守护的命令和「在不在跑」换成替身；画面服务是一个本机的 Unix 套接字。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from atlas_connect.audit import AuditLog
from atlas_connect.browser import BrowserDesk, IdleReaper
from atlas_connect.config import BrowserSettings, LocalPolicy, Paths
from atlas_devicelink import BROWSER, VIEW, Mux
from cn_kit import linked, policy


def desk(tmp_path: Path, *, missing: list[str] | None = None, **browser: Any) -> BrowserDesk:
    paths = Paths(tmp_path / ".atlas")
    paths.ensure()
    pol = policy(tmp_path, browser=BrowserSettings(**browser))
    d = BrowserDesk(pol, paths, AuditLog(paths.audit))
    d.probe.missing = lambda: list(missing or [])  # type: ignore[method-assign]
    return d


class FakeLauncher:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.stopped = 0
        self.started = 0

    async def ensure(self) -> bool:
        if self.running:
            return False
        self.running, self.started = True, self.started + 1
        return True

    async def stop(self) -> bool:
        was, self.running = self.running, False
        self.stopped += was
        return was


class FakeDaemon:
    def __init__(self, slots: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.slots = slots or {}

    async def run(self, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        self.calls.append(args)
        if args[:2] == ("takeover", "open"):
            return {"target": "T1", "reused": False}
        if args[:2] == ("takeover", "close"):
            return {"closed": True}
        if args[:2] == ("clipboard", "get"):
            Path(args[-1]).write_text("复制的文字", encoding="utf-8")
        return {}

    async def slot_of(self, screen: str) -> int | None:
        return self.slots.get(screen)


async def ask(gw: Mux, head: dict[str, Any]) -> dict[str, Any]:
    ch = await gw.open(BROWSER, head)
    first = await ch.receive()
    assert isinstance(first, str)
    return json.loads(first)  # type: ignore[no-any-return]


# ------------------------------------------------------------------ 策略、报告、环境


def test_browser_section_defaults_and_parsing() -> None:
    assert LocalPolicy().browser == BrowserSettings()
    parsed = LocalPolicy.parse({"browser": {"remote_view": False, "idle_stop_s": 60, "slots": 0}})
    assert parsed.browser.remote_view is False
    assert parsed.browser.idle_stop_s == 60.0 and parsed.browser.slots == 1


def test_report_says_installed_running_and_remote_view(tmp_path: Path) -> None:
    report = desk(tmp_path, remote_view=False).report()
    assert report["installed"] is True and report["running"] is False
    assert report["remote_view"] is False
    missing = desk(tmp_path, missing=["xdotool"]).report()
    assert missing["installed"] is False and missing["missing"] == ["xdotool"]


def test_cli_env_only_for_sessions_that_use_the_browser(tmp_path: Path) -> None:
    d = desk(tmp_path)
    assert d.cli_env({"thread": "t1"}) == {}
    env = d.cli_env({"thread": "t1", "browser": {"session": "t1", "screen": "root-1"}})
    assert env["ATLAS_BROWSER_SESSION"] == "t1" and env["ATLAS_BROWSER_SCREEN"] == "root-1"
    assert env["ATLAS_CONNECT_SOCKET"] == str(tmp_path / ".atlas" / "run" / "connect.sock")
    assert env["ATLAS_BROWSER_DIR"] == str(tmp_path / ".atlas" / "browser")
    assert env["ATLAS_BROWSER_VNC_UNIX"] == "1"
    assert env["PATH"].startswith(str(tmp_path / ".atlas" / "bin"))
    # 没装组件：不给（browser 命令不在，模型看到的是「没有这个命令」）
    assert desk(tmp_path, missing=["Xvfb"]).cli_env({"thread": "t1", "browser": {}}) == {}


async def test_idle_reaper_waits_for_commands_views_and_takeovers() -> None:
    now = [0.0]
    launcher = FakeLauncher(running=True)
    reaper = IdleReaper(launcher, idle_s=60, clock=lambda: now[0])  # type: ignore[arg-type]
    now[0] = 30
    assert await reaper.tick() is False
    reaper.views = 1
    now[0] = 1000
    assert await reaper.tick() is False  # 有人在看画面
    reaper.views, reaper.takeovers = 0, {"a1"}
    assert await reaper.tick() is False  # 接管窗口还开着
    reaper.takeovers.clear()
    assert await reaper.tick() is True and launcher.stopped == 1
    assert await reaper.tick() is False  # 已经关了


# ------------------------------------------------------------------ 浏览器请求


async def test_takeover_open_and_close_go_through_the_policy(tmp_path: Path) -> None:
    d = desk(tmp_path)
    launcher, daemon = FakeLauncher(), FakeDaemon()
    d.launcher = launcher  # type: ignore[assignment]
    d.reaper.launcher = launcher  # type: ignore[assignment]
    d._daemon = daemon  # type: ignore[assignment]
    async with linked(d.on_browser) as gw:
        bad = await ask(gw, {"op": "takeover.open", "takeover": "a1", "url": "file:///etc/passwd"})
        assert bad["status"] == 400
        opened = await ask(
            gw,
            {
                "op": "takeover.open",
                "takeover": "a1",
                "thread": "t1",
                "screen": "root-1",
                "url": "https://www.zhipin.com/",
            },
        )
        assert opened == {"status": 200, "body": {"target": "T1", "reused": False}}
        assert launcher.started == 1 and d.reaper.takeovers == {"a1"}
        closed = await ask(gw, {"op": "takeover.close", "takeover": "a1"})
        assert closed["body"] == {"closed": True} and not d.reaper.takeovers
        weird = await ask(gw, {"op": "takeover.close", "takeover": "../x"})
        assert weird["status"] == 400
    assert daemon.calls == [
        ("takeover", "open", "a1", "https://www.zhipin.com/", "--screen", "root-1"),
        ("takeover", "close", "a1"),
    ]
    log = AuditLog(tmp_path / ".atlas" / "audit.log").tail()
    assert any("开接管窗口" in line for line in log)


async def test_remote_view_off_refuses_takeover_and_clipboard(tmp_path: Path) -> None:
    d = desk(tmp_path, remote_view=False)
    d.launcher = FakeLauncher(running=True)  # type: ignore[assignment]
    async with linked(d.on_browser) as gw:
        for head in (
            {"op": "takeover.open", "takeover": "a1", "url": "https://a.test/"},
            {"op": "clipboard.get"},
        ):
            answer = await ask(gw, head)
            assert answer["status"] == 403
            assert answer["body"]["error"]["kind"] == "remote_view_off"
    not_installed = desk(tmp_path, missing=["Xvfb"])
    async with linked(not_installed.on_browser) as gw:
        answer = await ask(gw, {"op": "takeover.open", "takeover": "a1", "url": "https://a.test/"})
        assert answer["body"]["error"]["kind"] == "browser_not_installed"
        assert "atlas-connect browser install" in answer["body"]["error"]["message"]


async def test_clipboard_and_status(tmp_path: Path) -> None:
    d = desk(tmp_path)
    d.launcher = FakeLauncher(running=True)  # type: ignore[assignment]
    daemon = FakeDaemon({"root-1": 2})
    d._daemon = daemon  # type: ignore[assignment]
    async with linked(d.on_browser) as gw:
        assert (await ask(gw, {"op": "clipboard.set", "text": "密码"}))["status"] == 200
        got = await ask(gw, {"op": "clipboard.get"})
        assert got["body"] == {"text": "复制的文字"}
        status = await ask(gw, {"op": "status", "screen": "root-1"})
        assert status["body"]["running"] is True and status["body"]["slot"] == 2
    # 文字经文件进出，用完就删
    assert not list((tmp_path / ".atlas" / "browser").glob("clip-*"))


# ------------------------------------------------------------------ 画面流


async def test_view_splices_to_the_conversation_block() -> None:
    # ★ AF_UNIX 的路径不能太长（macOS 104 字节）：pytest 的临时目录装不下，换成 /tmp 下的短路径
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="cn-") as tmp:
        home = Path(tmp)
        d = desk(home)
        d.launcher = FakeLauncher(running=True)  # type: ignore[assignment]
        d._daemon = FakeDaemon({"root-1": 1})  # type: ignore[assignment]
        root = home / ".atlas" / "browser"
        root.mkdir(parents=True, exist_ok=True)
        got: list[bytes] = []

        async def vnc(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            got.append(await reader.readexactly(12))
            writer.close()

        server = await asyncio.start_unix_server(vnc, path=str(root / "vnc-1.sock"))
        try:
            async with linked(d.on_view) as gw:
                ch = await gw.open(VIEW, {"thread": "t1", "screen": "root-1"})
                assert await ch.receive() == b"RFB 003.008\n"
                assert d.reaper.views == 1
                await ch.send(b"RFB 003.008\n")
                assert await ch.receive() is None
                assert (ch.close_code, ch.close_reason) == (4502, "画面服务断了")
                never = await gw.open(VIEW, {"thread": "t2", "screen": "root-9"})
                assert await never.receive() is None
                assert never.close_code == 4404
            assert got == [b"RFB 003.008\n"] and d.reaper.views == 0
        finally:
            server.close()


@pytest.mark.parametrize(
    ("remote_view", "running", "code"), [(False, True, 4403), (True, False, 4404)]
)
async def test_view_refused_with_a_reason(
    tmp_path: Path, remote_view: bool, running: bool, code: int
) -> None:
    d = desk(tmp_path, remote_view=remote_view)
    d.launcher = FakeLauncher(running=running)  # type: ignore[assignment]
    async with linked(d.on_view) as gw:
        ch = await gw.open(VIEW, {"thread": "t1", "screen": "root-1"})
        assert await ch.receive() is None
        assert ch.close_code == code and ch.close_reason
