"""浏览器守护里不碰浏览器的部分：前后比较、站点匹配、cookie 形状、进程参数、命令行。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from atlas_browserd.__main__ import _parser, main
from atlas_browserd.cdp import _cookie_input, origin_of
from atlas_browserd.paths import Paths
from atlas_browserd.screens import ScreenTable, Slot
from atlas_browserd.spec import ProcessSpec
from atlas_browserd.state import SiteMatch
from atlas_browserd.windows import SiteDiff


def _c(domain: str, name: str, value: str) -> dict[str, str]:
    return {"domain": domain, "name": name, "value": value}


def test_site_diff_counts_new_and_changed_cookies_only() -> None:
    before = [_c(".taobao.com", "cna", "1"), _c(".tmall.com", "x", "1"), _c("ads.com", "id", "9")]
    after = [
        _c(".taobao.com", "cna", "1"),  # 没变
        _c(".taobao.com", "cookie2", "new"),  # 新增
        _c("login.taobao.com", "_tb_token_", "t"),  # 新增
        _c(".tmall.com", "x", "2"),  # 变了
        # ads.com 的被删了（退出登录那种）：不算
    ]
    assert SiteDiff.changed(before, after) == ["login.taobao.com", "taobao.com", "tmall.com"]
    assert SiteDiff.changed([], [_c("a.com", "n", "v")]) == ["a.com"]
    assert SiteDiff.changed(after, after) == []


def test_site_match_is_the_site_or_its_subdomains() -> None:
    match = SiteMatch(["taobao.com"])
    assert match.domain(".taobao.com") and match.domain("login.taobao.com")
    assert match.domain("TAOBAO.COM.")
    assert not match.domain("nottaobao.com") and not match.domain("taobao.com.evil.cn")
    assert match.origin("https://www.taobao.com:8443")
    assert not match.origin("https://evil.com/?taobao.com")
    assert match.common_origins() == {"https://taobao.com", "https://www.taobao.com"}


def test_cookie_input_normalises_storage_state_cookies() -> None:
    cookie = _cookie_input(
        {"name": "n", "value": "v", "domain": ".a.com", "expires": 0, "sameSite": "lax"}
    )
    assert cookie == {
        "name": "n",
        "value": "v",
        "domain": ".a.com",
        "path": "/",
        "httpOnly": False,
        "secure": False,
        "expires": -1,
    }
    assert _cookie_input({"name": "n", "value": "v"}) is None


def test_origin_of() -> None:
    assert origin_of("https://www.taobao.com/a?b") == "https://www.taobao.com"
    assert origin_of("http://127.0.0.1:8080/x") == "http://127.0.0.1:8080"
    assert origin_of("https://a.com:443/") == "https://a.com"
    assert origin_of("about:blank") is None
    assert origin_of("chrome://newtab/") is None


def test_process_spec(tmp_path: Path) -> None:
    spec = ProcessSpec(paths=Paths(tmp_path), view_key="k3y", chromium="/usr/bin/chromium")
    args = spec.chromium_args()
    assert "--remote-debugging-address=127.0.0.1" in args and "--no-sandbox" in args
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    ws = spec.websockify()
    assert ws is not None and "BasicHTTPAuth" in ws and "atlas:k3y" in ws
    # 没有画面口令就不开画面
    assert ProcessSpec(paths=Paths(tmp_path), view_key=None, chromium="c").websockify() is None
    assert "ATLAS_BROWSER_VIEW_KEY" not in spec.env()


def test_device_spec_listens_on_unix_sockets_and_keeps_chrome_sandbox(tmp_path: Path) -> None:
    """电脑上：画面服务只听守护目录里的 Unix 套接字，不开 websockify；Chrome 的沙盒照常开着。"""
    spec = ProcessSpec(
        paths=Paths(tmp_path),
        view_key=None,
        chromium="/usr/bin/google-chrome",
        no_sandbox=False,
        vnc_unix=True,
    )
    assert "--no-sandbox" not in spec.chromium_args()
    vnc = spec.x11vnc(Slot(1))
    assert vnc[vnc.index("-unixsock") + 1] == str(tmp_path / "vnc-1.sock")
    assert vnc[vnc.index("-rfbport") + 1] == "0" and "-localhost" not in vnc
    assert (
        ProcessSpec(paths=Paths(tmp_path), view_key="k", chromium="c", vnc_unix=True).websockify()
        is None
    )


def test_screen_is_split_into_slots_each_with_its_own_view(tmp_path: Path) -> None:
    """屏幕横排 3 块；每块一个 x11vnc 只截自己那块；websockify 按 token 转到那块。"""
    spec = ProcessSpec(paths=Paths(tmp_path), view_key="k3y", slots=3, chromium="c")
    assert spec.xvfb()[spec.xvfb().index("-screen") + 2] == "5760x1080x24"
    assert "--window-size=1920,1080" in spec.chromium_args()
    second = spec.x11vnc(Slot(1))
    assert second[second.index("-clip") + 1] == "1920x1080+1920+0"
    assert second[second.index("-rfbport") + 1] == "5901" and "-localhost" in second
    assert spec.view_tokens().splitlines() == [
        "slot0: 127.0.0.1:5900",
        "slot1: 127.0.0.1:5901",
        "slot2: 127.0.0.1:5902",
    ]
    ws = spec.websockify()
    assert ws is not None and str(tmp_path / "view-tokens") in ws and "TokenFile" in ws
    assert Slot(2).bounds == {"left": 3840, "top": 0, "width": 1920, "height": 1080}
    assert Slot.at(3900, slots=3) == Slot(2) and Slot.at(99999, slots=3) == Slot(2)


def test_screen_table_gives_each_conversation_its_own_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对话各占一块、再要还是同一块；只查不分；满了让出最久没用的那块。"""
    clock = iter(range(100))
    monkeypatch.setattr("atlas_browserd.screens.time.time", lambda: next(clock))
    table = ScreenTable(Paths(tmp_path), slots=2)
    assert table.find("A") is None
    a, b = table.acquire("A"), table.acquire("B")
    assert {a.index, b.index} == {0, 1}
    assert table.acquire("A") == a  # A 刚用过，B 成了最久没用的
    c = table.acquire("C")
    assert c == b and table.find("B") is None and table.find("A") == a
    # 块数改小了：超出的记录作废
    assert ScreenTable(Paths(tmp_path), slots=1).find("C") is None


def test_parser_covers_the_platform_commands() -> None:
    p = _parser()
    args = p.parse_args(["state", "export", "--sites", "a.com,b.com", "--out", "/tmp/x.json"])
    assert (args.command, args.action, args.sites) == ("state", "export", "a.com,b.com")
    args = p.parse_args(["takeover", "close", "t1", "--out", "/tmp/y.json"])
    assert (args.action, args.id) == ("close", "t1")
    with pytest.raises(SystemExit):
        p.parse_args(["takeover", "open", "t1"])
    args = p.parse_args(["takeover", "open", "t1", "https://x.com", "--screen", "conv-1"])
    assert args.screen == "conv-1"
    assert p.parse_args(["screen", "slot", "conv-1"]).key == "conv-1"
    assert p.parse_args(["windows", "keep"]).action == "keep"


def test_failed_command_reports_the_reason_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["state", "import", "/nonexistent/file.json"]) == 1
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "FileNotFoundError" in result["error"]


def test_clipboard_moves_text_through_files_and_removes_the_pasted_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """粘贴的常常是密码：放进剪贴板后文件就删掉；复制的写成 0600 的文件。中文原样进出。"""
    from atlas_browserd import clipboard

    board: dict[str, str] = {}
    monkeypatch.setattr(clipboard, "write_text", lambda text: board.update(text=text))
    monkeypatch.setattr(clipboard, "read_text", lambda: board.get("text", ""))

    pasted = tmp_path / "in.txt"
    pasted.write_text("验证码 4096", encoding="utf-8")
    assert main(["clipboard", "set", str(pasted)]) == 0
    assert board == {"text": "验证码 4096"} and not pasted.exists()

    out = tmp_path / "out.txt"
    assert main(["clipboard", "get", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "验证码 4096"
    assert out.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["chars"] == 8


def test_popup_keeper_moves_popups_into_the_openers_slot() -> None:
    """弹窗开在第 0 块、弹出它的窗口在第 2 块：挪到第 2 块（大小不超过一块）；新标签页不动。"""
    import asyncio

    from atlas_browserd.windows import PopupKeeper

    class FakeCdp:
        def __init__(self) -> None:
            self.windows = {
                "opener": (7, {"left": 3840, "top": 0, "width": 1920, "height": 1080}),
                "popup": (8, {"left": 0, "top": 0, "width": 2400, "height": 600}),
                "tab": (7, {"left": 3840, "top": 0, "width": 1920, "height": 1080}),
            }
            self.moved: list[tuple[int, dict[str, int]]] = []

        async def window_of(self, target: str) -> tuple[int, dict[str, int]]:
            return self.windows[target]

        async def move_window(self, window_id: int, bounds: dict[str, int]) -> None:
            self.moved.append((window_id, bounds))

    cdp = FakeCdp()
    keeper = PopupKeeper(cdp)  # type: ignore[arg-type]
    keeper.SETTLE_S = 0
    asyncio.run(keeper.rehome("popup", "opener"))
    asyncio.run(keeper.rehome("tab", "opener"))
    assert cdp.moved == [(8, {"left": 3840, "top": 0, "width": 1920, "height": 600})]


# ---------------------------------------------------------------------------- computer use


def test_coordinates_map_into_the_conversation_block_only() -> None:
    from atlas_browserd.screen import CoordinateMap, OutOfBlock

    block = CoordinateMap(Slot(1))
    # 设计稿的算例：(750, 257) → (1920 + 750 × 1.5, 257 × 1.5)
    assert block.to_screen(750, 257) == (3045, 386)
    assert block.to_screen(0, 0) == (1920, 0)
    for x, y in ((1280, 10), (-1, 10), (10, 720), (10, -0.5)):
        with pytest.raises(OutOfBlock):
            block.to_screen(x, y)


def test_screen_actions_parse_from_the_command_line() -> None:
    from atlas_browserd.screen import ScreenAction

    assert ScreenAction.parse(["tap", "750", "257"]).at == (750, 257)
    assert ScreenAction.parse(["tap", "(750,", "257)"]).at == (750, 257)
    drag = ScreenAction.parse(["drag", "10", "20", "300", "20"])
    assert (drag.at, drag.to) == ((10, 20), (300, 20))
    scroll = ScreenAction.parse(["scroll", "600", "400", "down"])
    assert (scroll.text, scroll.times) == ("down", 5)
    assert ScreenAction.parse(["key", "ctrl+Enter"]).text == "ctrl+Return"
    assert ScreenAction.parse(["key", "ArrowDown"]).text == "Down"
    assert ScreenAction.parse(["paste", "你好", "世界"]).text == "你好 世界"
    assert ScreenAction.parse(["go", "https://www.zhipin.com/"]).kind == "go"
    for bad in (["tap", "1"], ["tap", "a", "b"], ["scroll", "1", "2", "sideways"], ["key"]):
        with pytest.raises(ValueError):
            ScreenAction.parse(bad)


def test_screen_desk_refuses_out_of_block_before_touching_input(tmp_path: Path) -> None:
    """越界的坐标在排队、置前、动键鼠之前就拒绝。"""
    from atlas_browserd.screen import OutOfBlock, ScreenDesk

    class Boom:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"不该动键鼠：{name}")

    desk = ScreenDesk(
        Paths(tmp_path), target="t", slot=Slot(2), out=tmp_path, xinput=Boom(), capture=Boom()
    )  # type: ignore[arg-type]
    with pytest.raises(OutOfBlock):
        desk.run(["tap", "1300", "10"])


def test_daemon_cli_knows_stop_and_optional_close_out() -> None:
    args = _parser().parse_args(["takeover", "close", "t-1"])
    assert args.out is None
    assert _parser().parse_args(["stop"]).command == "stop"


def test_profile_never_offers_to_translate_and_downloads_without_asking(tmp_path: Path) -> None:
    from atlas_browserd.supervisor import quiet_profile

    paths, downloads = Paths(tmp_path), tmp_path / "Downloads"
    quiet_profile(paths, downloads)  # 新 profile：先写一份
    prefs = tmp_path / "profile" / "Default" / "Preferences"
    wanted = {
        "translate": {"enabled": False},
        "download": {
            "default_directory": str(downloads),
            "prompt_for_download": False,
            "directory_upgrade": True,
        },
        "profile": {"default_content_setting_values": {"automatic_downloads": 1}},
    }
    assert json.loads(prefs.read_text()) == wanted
    assert downloads.is_dir()
    prefs.write_text(json.dumps({"translate": {"enabled": True}, "profile": {"name": "x"}}))
    quiet_profile(paths, downloads)  # 已有的：只改这几项
    data = json.loads(prefs.read_text())
    assert data["profile"]["name"] == "x" and data["translate"] == {"enabled": False}
    assert data["profile"]["default_content_setting_values"] == {"automatic_downloads": 1}


class _FakeXdotool:
    """记下 xdotool 的调用；getmouselocation 报当前位置，mousemove 改位置。"""

    def __init__(self, at: tuple[int, int] = (0, 0), *, hang: str = "") -> None:
        self.at, self.calls, self.hang = at, [], hang

    def __call__(self, argv: list[str], **_kw: object) -> object:
        import subprocess
        from types import SimpleNamespace

        args = argv[1:]
        self.calls.append(args)
        if args[0] == self.hang:
            raise subprocess.TimeoutExpired(argv, 10)
        if args[0] == "getmouselocation":
            out = f"X={self.at[0]}\nY={self.at[1]}\nSCREEN=0\nWINDOW=1\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[0] == "mousemove":
            self.at = (int(args[-2]), int(args[-1]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _moves(fake: _FakeXdotool) -> list[list[str]]:
    return [c for c in fake.calls if c[0] == "mousemove"]


def test_tapping_the_same_spot_twice_does_not_wait_for_a_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """鼠标已经在目标上时 mousemove --sync 等不到移动事件、卡满超时：已经在那儿就不挪。"""
    from atlas_browserd import screen

    fake = _FakeXdotool()
    monkeypatch.setattr(screen.subprocess, "run", fake)
    xinput = screen.XInput()
    xinput.click(1310, 440)
    xinput.click(1310, 440)  # 「再点一次即可下载」
    xinput.scroll(1310, 440, "down", 3)
    assert len(_moves(fake)) == 1
    assert sum(c[0] == "click" for c in fake.calls) == 3


def test_a_short_drag_never_moves_onto_the_spot_it_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlas_browserd import screen

    fake = _FakeXdotool()
    monkeypatch.setattr(screen.subprocess, "run", fake)
    screen.XInput().drag((100, 100), (103, 100))  # 12 步挪 3 个像素：相邻几步落在同一点
    targets = [tuple(c[-2:]) for c in _moves(fake)]
    assert targets == [("100", "100"), ("101", "100"), ("102", "100"), ("103", "100")]


def test_a_stuck_xdotool_is_reported_as_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时报成普通的失败（browser 命令打一行原因），不让 Traceback 出现在模型看的输出里。"""
    from atlas_browserd import screen

    monkeypatch.setattr(screen.subprocess, "run", _FakeXdotool(hang="click"))
    with pytest.raises(RuntimeError, match="超时"):
        screen.XInput().click(5, 5)


def test_downloads_say_what_finished_what_is_stuck_and_what_is_an_error_page(
    tmp_path: Path,
) -> None:
    import os

    from atlas_browserd.downloads import describe, scan

    now = 1_000_000.0
    files = {
        "M500002obykg1VsjK8.mp3": (b"ID3\x03" + b"\0" * 100, now - 300),
        "Unconfirmed 287984.crdownload": (b"\0" * 1024, now - 5),
        "song.mp3.crdownload": (b"\0" * 2048, now - 60),
        "next.mp3.crdownload": (b"\0" * 2048, now - 1),
        "圣诞星-周杰伦.mp3": (b"<!DOCTYPE html><html>error</html>", now - 30),
    }
    for name, (body, mtime) in files.items():
        (tmp_path / name).write_bytes(body)
        os.utime(tmp_path / name, (mtime, mtime))
    found = {d.name: d for d in scan(tmp_path, now=now)}
    assert found["M500002obykg1VsjK8.mp3"].state == "完成"
    assert not found["M500002obykg1VsjK8.mp3"].note
    assert found["Unconfirmed 287984.crdownload"].state == "等确认"
    assert found["song.mp3.crdownload"].state == "卡住"
    assert found["next.mp3.crdownload"].state == "下载中"
    assert "错误页" in found["圣诞星-周杰伦.mp3"].note
    text = describe(scan(tmp_path, now=now), tmp_path)
    assert text.index("next.mp3") < text.index("M500002")  # 新的在前


def test_waiting_for_downloads_stops_once_nothing_is_growing(tmp_path: Path) -> None:
    import os

    from atlas_browserd.downloads import wait

    part = tmp_path / "a.mp3.crdownload"
    part.write_bytes(b"x")
    clock = [100.0]
    os.utime(part, (clock[0], clock[0]))

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 106:  # 下完了：改名成正式文件
            part.rename(tmp_path / "a.mp3")

    files, waited = wait(60, tmp_path, clock=lambda: clock[0], sleep=sleep)
    assert [f.state for f in files] == ["完成"] and waited == 6


def test_downloads_command_needs_no_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from atlas_browserd import command, downloads

    def boom(*_a: object) -> None:
        raise AssertionError("不该去拉起浏览器")

    monkeypatch.setattr(command, "_ensure_daemon", boom)
    monkeypatch.setattr(downloads, "DOWNLOAD_DIR", tmp_path)
    assert command.main(["downloads"]) == 0
    assert "还没有文件" in capsys.readouterr().out
    assert command.main(["downloads", "--wait", "x"]) == 2
