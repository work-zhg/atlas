"""浏览器下载的文件：browser downloads [--wait 秒]。

下载目录在拉起浏览器之前写进 profile 的偏好（supervisor.quiet_profile）：固定在 DOWNLOAD_DIR，
不弹「另存为」，同一个网站连着下好几个文件也不弹确认。

★ 为什么要这个子命令（会话 01a1f830）：模型找不到文件下到哪了，花 6 分钟翻 Chrome 的历史库；
  一个停在 1MB 的 Unconfirmed *.crdownload 被当成还在下，等了好几分钟；网站返回的错误页被存成了
  .mp3。这里一次说清：完成了没有、卡住没有、存下来的是不是网页。
★ 只看目录，不碰浏览器：不经调试接口（检测自动化的网站认不出来），也不用等守护起来。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DOWNLOAD_DIR", "Download", "describe", "scan", "wait"]

DOWNLOAD_DIR = Path(os.environ.get("ATLAS_BROWSER_DOWNLOADS", str(Path.home() / "Downloads")))
#: Chrome 下载中的文件后缀；文件名还没定下来时叫 Unconfirmed <编号>.crdownload
PARTIAL = ".crdownload"
UNCONFIRMED = "Unconfirmed "
#: 下载中的文件这么久没变大，算卡住
STALL_S = 20.0
#: 列出最近多少个
LIMIT = 20
_HTML_SUFFIXES = {".html", ".htm", ".mhtml", ".xhtml"}


@dataclass(frozen=True)
class Download:
    name: str
    size: int
    mtime: float
    #: 完成 / 下载中 / 卡住 / 等确认
    state: str
    note: str = ""


def scan(directory: Path = DOWNLOAD_DIR, *, now: float | None = None) -> list[Download]:
    """目录里的文件，新的在前。目录不存在返回空。"""
    if not directory.is_dir():
        return []
    now = time.time() if now is None else now
    found = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        found.append(_classify(path, stat.st_size, stat.st_mtime, now))
    return sorted(found, key=lambda d: d.mtime, reverse=True)


def _classify(path: Path, size: int, mtime: float, now: float) -> Download:
    name = path.name
    if name.endswith(PARTIAL):
        if name.startswith(UNCONFIRMED):
            return Download(
                name, size, mtime, "等确认",
                "Chrome 还没定下文件名，多半在等下载确认：看一眼截图，右上角下载气泡里点「保留」",
            )  # fmt: skip
        if now - mtime >= STALL_S:
            return Download(
                name, size, mtime, "卡住",
                f"{int(now - mtime)} 秒没变大：重新点一次下载，或者换一条下载线路",
            )  # fmt: skip
        return Download(name, size, mtime, "下载中")
    if path.suffix.lower() not in _HTML_SUFFIXES and _looks_like_html(path):
        return Download(name, size, mtime, "完成", "内容是网页、不是文件本身：多半是错误页")
    return Download(name, size, mtime, "完成")


def _looks_like_html(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(512)
    except OSError:
        return False
    head = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html"))


def wait(
    seconds: float,
    directory: Path = DOWNLOAD_DIR,
    *,
    poll_s: float = 2.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Download], float]:
    """等到没有「下载中」的文件（都完成、或者卡住了），最多等 seconds 秒。→ (文件, 等了多久)。"""
    start = clock()
    while True:
        files = scan(directory, now=clock())
        waited = clock() - start
        if not any(f.state == "下载中" for f in files) or waited >= seconds:
            return files, waited
        sleep(poll_s)


def describe(files: list[Download], directory: Path = DOWNLOAD_DIR) -> str:
    """给模型看的清单。"""
    if not files:
        return f"下载目录 {directory} 里还没有文件"
    shown = files[:LIMIT]
    lines = [f"下载目录 {directory}（最近 {len(shown)} 个，新的在前）："]
    for f in shown:
        stamp = time.strftime("%H:%M:%S", time.localtime(f.mtime))
        line = f"- {f.name}  {_size(f.size)}  {stamp}  {f.state}"
        lines.append(f"{line}（{f.note}）" if f.note else line)
    if len(files) > LIMIT:
        lines.append(f"……还有 {len(files) - LIMIT} 个更早的")
    lines.append("文件名取自下载链接，未必和网页上的标题一样：拷进工作区时按需要重命名。")
    return "\n".join(lines)


def _size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"
