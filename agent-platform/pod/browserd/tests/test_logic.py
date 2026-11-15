"""浏览器守护里不碰浏览器的部分：前后比较、站点匹配、cookie 形状、进程参数、命令行。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from atlas_browserd.__main__ import _parser, main
from atlas_browserd.cdp import _cookie_input, origin_of
from atlas_browserd.paths import Paths
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
    assert "-localhost" in spec.x11vnc()
    ws = spec.websockify()
    assert ws is not None and "BasicHTTPAuth" in ws and "atlas:k3y" in ws
    # 没有画面口令就不开画面
    assert ProcessSpec(paths=Paths(tmp_path), view_key=None, chromium="c").websockify() is None
    assert "ATLAS_BROWSER_VIEW_KEY" not in spec.env()


def test_parser_covers_the_platform_commands() -> None:
    p = _parser()
    args = p.parse_args(["state", "export", "--sites", "a.com,b.com", "--out", "/tmp/x.json"])
    assert (args.command, args.action, args.sites) == ("state", "export", "a.com,b.com")
    args = p.parse_args(["takeover", "close", "t1", "--out", "/tmp/y.json"])
    assert (args.action, args.id) == ("close", "t1")
    with pytest.raises(SystemExit):
        p.parse_args(["takeover", "open", "t1"])


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
