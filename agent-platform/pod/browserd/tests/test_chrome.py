"""对着真的 Chrome（本机）：登录态进出、清站点、接管窗口。

本机没有 Chrome 或 Playwright 时跳过。不联网：本地存储走临时页面（请求都由路由回空页面），
cookie 直接写；接管窗口打开的地址打不开也不要紧，比较的是 cookie。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright")

CHROME = next(
    (
        p
        for p in (
            os.environ.get("ATLAS_TEST_CHROME", ""),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("chromium") or "",
            shutil.which("google-chrome") or "",
        )
        if p and Path(p).exists()
    ),
    None,
)
pytestmark = pytest.mark.skipif(CHROME is None, reason="本机没有 Chrome")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def chrome(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    port = _free_port()
    profile = tmp_path_factory.mktemp("profile")
    proc = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    from atlas_browserd.supervisor import cdp_version

    while cdp_version(port) is None:
        assert time.monotonic() < deadline, "Chrome 没起来"
        time.sleep(0.2)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
async def cdp(chrome: int) -> AsyncIterator[object]:
    from atlas_browserd.cdp import connect

    async with connect(chrome) as session:
        yield session


def _cookie(domain: str, name: str, value: str) -> dict[str, object]:
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1}


async def test_import_export_and_forget(cdp: object) -> None:
    from atlas_browserd.state import StateIO

    io = StateIO(cdp)  # type: ignore[arg-type]
    bundle = {
        "cookies": [_cookie(".shop.test", "sid", "s1"), _cookie(".ads.test", "id", "a1")],
        "origins": [
            {"origin": "https://www.shop.test", "localStorage": [{"name": "token", "value": "t1"}]}
        ],
    }
    assert await io.import_(bundle) == {"cookies": 2, "origins": 1}
    exported = await io.export(["shop.test"], ["https://www.shop.test"])
    assert [(c["domain"], c["name"]) for c in exported["cookies"]] == [(".shop.test", "sid")]
    assert exported["origins"] == [
        {"origin": "https://www.shop.test", "localStorage": [{"name": "token", "value": "t1"}]}
    ]
    await io.forget("shop.test")
    after = await io.export(["shop.test"], ["https://www.shop.test"])
    assert after == {"cookies": [], "origins": []}
    # 别的站点不受影响
    assert [c["name"] for c in (await io.export(["ads.test"]))["cookies"]] == ["id"]


async def test_takeover_window_reports_the_sites_logged_into(cdp: object, tmp_path: Path) -> None:
    from atlas_browserd.paths import Paths
    from atlas_browserd.windows import TakeoverWindows

    paths = Paths(tmp_path)
    paths.ensure()
    windows = TakeoverWindows(cdp, paths)  # type: ignore[arg-type]
    await cdp.add_cookies([_cookie(".bank.test", "old", "1")])  # type: ignore[attr-defined]
    opened = await windows.open("t-1", "about:blank")
    assert opened["reused"] is False
    again = await windows.open("t-1", "about:blank")
    assert again == {"target": opened["target"], "reused": True}
    # 人在窗口里登录：网站写下新的 cookie
    await cdp.add_cookies(  # type: ignore[attr-defined]
        [_cookie("login.bank.test", "session", "s"), _cookie(".bank.test", "old", "1")]
    )
    closed = await windows.close("t-1")
    assert closed["domains"] == ["login.bank.test"]
    assert any(c["name"] == "session" for c in closed["state"]["cookies"])
    assert not (paths.takeovers / "t-1.json").exists()
    assert await cdp.target_url(opened["target"]) is None  # type: ignore[attr-defined]
