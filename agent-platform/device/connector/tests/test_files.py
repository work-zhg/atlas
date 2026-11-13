"""本机文件：路径把关、列目录、列允许目录、列改动、取文件（整个、Range、304）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from atlas_connect.audit import AuditLog
from atlas_connect.files import FileDesk
from atlas_devicelink import FILE, Mux
from cn_kit import linked, policy


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "code"
    proj = root / "shop"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.ts").write_text("export const a = 1;\n")
    (proj / ".env").write_text("SECRET=1")
    (proj / "server.pem").write_text("key")
    (proj / "README.md").write_text("hello world")
    (root / "atlas").mkdir()
    (root / ".hidden").mkdir()
    (tmp_path / "outside.txt").write_text("nope")
    (proj / "escape").symlink_to(tmp_path)
    return proj


async def ask(gw: Mux, head: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    ch = await gw.open(FILE, head)
    first = await ch.receive()
    assert isinstance(first, str)
    body = b""
    async for item in ch:
        body += item if isinstance(item, bytes) else item.encode()
    return json.loads(first), body


async def test_reads_go_through_the_guard(project: Path, tmp_path: Path) -> None:
    desk = FileDesk(policy(project.parent), AuditLog(tmp_path / "audit.log"))
    base = {"op": "fetch", "project_dir": str(project)}
    async with linked(desk.on_file) as gw:
        head, body = await ask(gw, {**base, "path": "src/app.ts"})
        assert head["status"] == 200 and body == b"export const a = 1;\n"
        assert head["headers"]["Content-Length"] == str(len(body))
        for path, status in [
            ("../atlas", 403),  # 整理后不在项目目录里
            (".env", 403),  # 以点开头
            ("server.pem", 403),  # 本机拒绝规则
            ("escape/outside.txt", 403),  # 符号链接解析后在目录外
            ("missing.txt", 404),
        ]:
            head, _ = await ask(gw, {**base, "path": path})
            assert head["status"] == status, path
    log = "\n".join(AuditLog(tmp_path / "audit.log").tail())
    assert "读文件" in log and "拒绝读文件" in log


async def test_range_and_not_modified(project: Path, tmp_path: Path) -> None:
    desk = FileDesk(policy(project.parent), AuditLog(tmp_path / "a.log"))
    base = {"op": "fetch", "project_dir": str(project), "path": "README.md"}
    async with linked(desk.on_file) as gw:
        head, body = await ask(gw, {**base, "byte_range": "bytes=6-10"})
        assert head["status"] == 206 and body == b"world"
        assert head["headers"]["Content-Range"] == "bytes 6-10/11"
        etag = head["headers"]["ETag"]
        head, body = await ask(gw, {**base, "if_none_match": etag})
        assert head["status"] == 304 and body == b""


async def test_list_dir_hides_dot_and_denied(project: Path, tmp_path: Path) -> None:
    desk = FileDesk(policy(project.parent), AuditLog(tmp_path / "a.log"))
    async with linked(desk.on_file) as gw:
        head, _ = await ask(gw, {"op": "list", "project_dir": str(project), "path": ""})
    names = [e["name"] for e in head["body"]["entries"]]
    assert names == ["escape", "src", "README.md"]  # 目录在前；.env、server.pem 不列


async def test_roots_lists_one_level_of_project_names(project: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    root = project.parent
    desk = FileDesk(policy(root, remote_files=False), AuditLog(tmp_path / "a.log"))
    async with linked(desk.on_file) as gw:
        head, _ = await ask(gw, {"op": "roots"})
    entries = head["body"]["roots"][0]["entries"]
    # 不受远程查看开关限制；以点开头的不列；标出 git 仓库
    assert [(e["name"], e["is_repo"]) for e in entries] == [("atlas", False), ("shop", True)]
    assert entries[1]["path"] == f"{root}/shop"


async def test_changes_from_git(project: Path, tmp_path: Path) -> None:
    git = ["git", "-C", str(project), "-c", "user.email=a@b", "-c", "user.name=a"]
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run([*git, "add", "README.md", "src/app.ts"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (project / "src" / "app.ts").write_text("export const a = 2;\n")
    (project / "src" / "new.ts").write_text("x")
    (project / "README.md").unlink()
    desk = FileDesk(policy(project.parent), AuditLog(tmp_path / "a.log"))
    async with linked(desk.on_file) as gw:
        head, _ = await ask(gw, {"op": "changes", "project_dir": str(project)})
    changes = {e["path"]: e["status"] for e in head["body"]["entries"]}
    assert (
        changes["src/app.ts"] == "M"
        and changes["src/new.ts"] == "A"
        and changes["README.md"] == "D"
    )
    assert ".env" not in changes  # 以点开头的不列


async def test_remote_files_off_refuses_reads(project: Path, tmp_path: Path) -> None:
    desk = FileDesk(policy(project.parent, remote_files=False), AuditLog(tmp_path / "a.log"))
    async with linked(desk.on_file) as gw:
        head, _ = await ask(gw, {"op": "list", "project_dir": str(project)})
    assert head["status"] == 403 and "关闭了远程查看文件" in head["message"]
