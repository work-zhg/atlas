"""本机文件（c4-model/agent/connector/code-connector-files.html）。

    FileDesk     设备网关开的文件流：按 op 分给列改动、列目录、列允许目录、取文件；全部只读
    PathGuard    本机把关，只写这一处：不信任平台给的路径
    GitChanges   还没提交的改动交给 git 算，结果缓存一两秒
    RootLister   选项目用：每个允许目录下一层的子目录名

文件流的格式（与设备网关的 FileRelay 对应）：首条是 JSON 文本的响应头
    {"status": 200, "body": {...}}                         列改动、列目录、列允许目录
    {"status": 200 | 206 | 304, "headers": {...}}          取文件，之后是内容块（二进制，64 KB）
    {"status": 403 | 404, "message": "..."}                把关不通过、文件不在
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from email.utils import formatdate
from pathlib import Path, PurePosixPath
from typing import Any

from atlas_devicelink import Channel, ChannelClosed

from .audit import AuditLog
from .config import LocalPolicy
from .session import ProjectDir, Rejected

__all__ = ["FileDesk", "GitChanges", "PathGuard", "RootLister", "file_version"]

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
LIST_LIMIT = 500
ROOT_LIMIT = 200
GIT_CACHE_S = 1.5
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Denied(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def file_version(st: os.stat_result) -> str:
    """文件的版本：按大小和修改时间算，不读内容。"""
    return '"' + hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16] + '"'


def _hidden(name: str) -> bool:
    return name.startswith(".")


class PathGuard:
    """顺序固定：远程查看关着 → 整理路径 → 解析符号链接 → 必须仍在项目目录里
    → 以点开头 → 本机拒绝规则。"""

    def __init__(self, policy: LocalPolicy) -> None:
        self._policy = policy

    def project(self, raw: str) -> Path:
        if not self._policy.remote_files:
            raise Denied(403, "电脑主人关闭了远程查看文件")
        try:
            return ProjectDir.resolve(raw, self._policy, create=False)
        except Rejected as exc:
            raise Denied(403 if exc.reason == "dir_not_allowed" else 404, exc.message) from exc

    def resolve(self, project: Path, rel: str) -> Path:
        norm = os.path.normpath(rel.strip().lstrip("/")) if rel.strip() else "."
        parts = PurePosixPath(norm.replace(os.sep, "/")).parts
        if parts and parts[0] == "..":
            raise Denied(403, f"路径不在项目目录里：{rel}")
        for part in parts:
            if part != "." and _hidden(part):
                raise Denied(403, f"以点开头的不给看：{rel}")
            if self._policy.denied(part):
                raise Denied(403, f"这台电脑不允许远程查看：{rel}")
        target = (project / norm).resolve()
        if target != project and not target.is_relative_to(project):
            raise Denied(403, f"路径解析后不在项目目录里：{rel}")
        if not target.exists():
            raise Denied(404, f"没有这个文件：{rel}")
        return target


class GitChanges:
    """调用电脑上的 git 列还没提交的改动；不是 git 仓库返回 None。"""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[float, dict[str, Any] | None]] = {}

    async def status(self, project: Path) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._cache.get(project)
        if cached is not None and now - cached[0] < GIT_CACHE_S:
            return cached[1]
        result = await self._run(project)
        self._cache[project] = (now, result)
        return result

    async def _run(self, project: Path) -> dict[str, Any] | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(project),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        entries: list[dict[str, Any]] = []
        items = out.decode(errors="replace").split("\0")
        i = 0
        while i < len(items):
            item = items[i]
            i += 1
            if len(item) < 4:
                continue
            xy, path = item[:2], item[3:]
            if "R" in xy or "C" in xy:
                i += 1  # 改名：下一项是原来的路径
            if any(_hidden(p) for p in PurePosixPath(path).parts):
                continue
            status = (
                "A" if xy == "??" or "A" in xy else "D" if "D" in xy else "R" if "R" in xy else "M"
            )
            entry: dict[str, Any] = {"path": path, "status": status}
            if status != "D":
                try:
                    st = (project / path).stat()
                    entry |= {
                        "size": st.st_size,
                        "modified_at": st.st_mtime,
                        "version": file_version(st),
                    }
                except OSError:
                    pass
            entries.append(entry)
        entries.sort(key=lambda e: e["path"])
        return {"is_repo": True, "entries": entries}


class RootLister:
    """每个允许目录下一层的子目录名，标出是不是 git 仓库；不往下展开、不读内容，以点开头的不列。"""

    def __init__(self, policy: LocalPolicy) -> None:
        self._policy = policy

    def list(self) -> dict[str, Any]:
        roots = []
        for raw in self._policy.allowed_roots:
            root = Path(raw).expanduser()
            entries: list[dict[str, Any]] = []
            truncated = False
            if root.is_dir():
                subdirs = sorted(
                    (p for p in root.iterdir() if p.is_dir() and not _hidden(p.name)),
                    key=lambda p: p.name.lower(),
                )
                truncated = len(subdirs) > ROOT_LIMIT
                entries = [
                    {
                        "name": p.name,
                        "path": f"{raw.rstrip('/')}/{p.name}",
                        "is_repo": (p / ".git").exists(),
                    }
                    for p in subdirs[:ROOT_LIMIT]
                ]
            roots.append(
                {"root": raw, "exists": root.is_dir(), "entries": entries, "truncated": truncated}
            )
        return {"roots": roots}


def _list_dir(directory: Path, policy: LocalPolicy) -> dict[str, Any]:
    children = [p for p in directory.iterdir() if not _hidden(p.name) and not policy.denied(p.name)]
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    entries = []
    for p in children[:LIST_LIMIT]:
        try:
            st = p.stat()
        except OSError:
            continue
        is_dir = p.is_dir()
        entry: dict[str, Any] = {"name": p.name, "is_dir": is_dir, "modified_at": st.st_mtime}
        if not is_dir:
            entry |= {"size": st.st_size, "version": file_version(st)}
        entries.append(entry)
    return {"entries": entries, "truncated": len(children) > LIST_LIMIT}


def _parse_range(raw: str | None, size: int) -> tuple[int, int] | None:
    """单段 Range → [start, end]（含）。不合法或不满足返回 None（按整文件处理）。"""
    m = _RANGE.match((raw or "").strip())
    if not m or size == 0:
        return None
    start_s, end_s = m.groups()
    if not start_s and not end_s:
        return None
    if not start_s:
        n = int(end_s)
        return (max(0, size - n), size - 1) if n > 0 else None
    start = int(start_s)
    end = min(int(end_s), size - 1) if end_s else size - 1
    return (start, end) if start <= end and start < size else None


class FileDesk:
    def __init__(self, policy: LocalPolicy, audit: AuditLog) -> None:
        self._policy = policy
        self._guard = PathGuard(policy)
        self._git = GitChanges()
        self._roots = RootLister(policy)
        self._audit = audit

    async def on_file(self, channel: Channel) -> None:
        head = channel.head
        op = head.get("op")
        try:
            if op == "roots":
                await _answer(channel, 200, self._roots.list())
            elif op == "changes":
                project = self._guard.project(str(head.get("project_dir") or ""))
                status = await self._git.status(project)
                await _answer(channel, 200, status or {"is_repo": False, "entries": []})
            elif op == "list":
                project = self._guard.project(str(head.get("project_dir") or ""))
                directory = self._guard.resolve(project, str(head.get("path") or ""))
                if not directory.is_dir():
                    raise Denied(404, f"不是目录：{head.get('path')}")
                await _answer(channel, 200, _list_dir(directory, self._policy))
            elif op == "fetch":
                await self._fetch(channel, head)
            else:
                raise Denied(400, f"不认识的操作：{op}")
        except Denied as exc:
            if exc.status == 403:
                self._audit.record(
                    "拒绝读文件",
                    f"{head.get('project_dir')}/{head.get('path') or ''}（{exc.message}）",
                )
            await _send(
                channel,
                {
                    "status": exc.status,
                    "message": exc.message,
                    "body": {"error": {"message": exc.message}},
                },
            )
        except ChannelClosed:
            return
        finally:
            await channel.close()

    async def _fetch(self, channel: Channel, head: dict[str, Any]) -> None:
        project = self._guard.project(str(head.get("project_dir") or ""))
        target = self._guard.resolve(project, str(head.get("path") or ""))
        if not target.is_file():
            raise Denied(404, f"不是文件：{head.get('path')}")
        st = target.stat()
        version = file_version(st)
        headers: dict[str, Any] = {
            "ETag": version,
            "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            "Accept-Ranges": "bytes",
        }
        if head.get("if_none_match") == version:
            await _send(channel, {"status": 304, "headers": headers})
            return
        rng = _parse_range(head.get("byte_range"), st.st_size)
        start, end = rng or (0, st.st_size - 1)
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        if rng is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{st.st_size}"
        self._audit.record("读文件", f"{project}/{head.get('path')}（网页）")
        await _send(channel, {"status": 206 if rng is not None else 200, "headers": headers})
        with target.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = await asyncio.to_thread(f.read, min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await channel.send(chunk)


async def _answer(channel: Channel, status: int, body: dict[str, Any]) -> None:
    await _send(channel, {"status": status, "body": body})


async def _send(channel: Channel, head: dict[str, Any]) -> None:
    import json

    await channel.send(json.dumps(head, ensure_ascii=False))
