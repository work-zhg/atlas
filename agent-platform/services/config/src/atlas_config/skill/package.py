"""把上传的 zip / 本地目录读成内存里的技能包，同时做第 1 层（归档安全）检查。

★ 归档安全必须在**读的时候**做，不能读完再查：zip 炸弹要在解压前按声明大小
  拦住，符号链接要在跟随之前拦住。读完再查就已经晚了。
"""

from __future__ import annotations

import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Limits", "RawPackage", "ScanBlocked", "load_dir", "load_zip"]

#: 打包工具顺手塞进来的东西，静默丢掉 —— 拒绝它们只会让上传者困惑
_JUNK_PARTS = ("__MACOSX", ".DS_Store", "__pycache__", ".git")

#: 二进制可执行文件的魔数。技能里的脚本应当是可审查的源码，不是编译产物。
_BINARY_MAGIC = (
    b"\x7fELF",  # Linux
    b"MZ",  # Windows PE
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",  # Mach-O
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
)
_BINARY_EXT = (".so", ".dll", ".dylib", ".exe", ".bin", ".class", ".jar", ".pyc")


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    max_files: int
    max_file_bytes: int


class ScanBlocked(ValueError):
    """有 blocker 级命中：上传直接被拒，不生成任何记录。"""

    def __init__(self, layer: str, hits: list[str]) -> None:
        self.layer = layer
        self.hits = hits
        super().__init__(f"{layer} 检查未通过：" + "；".join(hits[:10]))


@dataclass
class RawPackage:
    files: dict[str, bytes] = field(default_factory=dict)
    executable: set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return sum(len(b) for b in self.files.values())


def _is_junk(path: str) -> bool:
    return any(part in _JUNK_PARTS for part in path.split("/"))


def _check_path(raw: str, hits: list[str]) -> str | None:
    if "\x00" in raw or "\\" in raw:
        hits.append(f"非法路径字符：{raw!r}")
        return None
    if raw.startswith("/"):
        hits.append(f"绝对路径：{raw}")
        return None
    norm = posixpath.normpath(raw)
    if norm.startswith("..") or "/../" in f"/{raw}/":
        hits.append(f"路径穿越：{raw}")
        return None
    return norm


def _check_binary(path: str, data: bytes, hits: list[str]) -> None:
    if path.lower().endswith(_BINARY_EXT) or data.startswith(_BINARY_MAGIC):
        hits.append(f"二进制可执行文件：{path}")


def _strip_single_root(pkg: RawPackage) -> RawPackage:
    """zip 常常多包一层目录（my-skill/SKILL.md）。只有一个顶层目录且根上没有
    SKILL.md 时剥掉它。"""
    if "SKILL.md" in pkg.files or not pkg.files:
        return pkg
    roots = {p.split("/", 1)[0] for p in pkg.files}
    if len(roots) != 1 or any("/" not in p for p in pkg.files):
        return pkg
    root = next(iter(roots)) + "/"
    return RawPackage(
        files={p.removeprefix(root): b for p, b in pkg.files.items()},
        executable={p.removeprefix(root) for p in pkg.executable},
    )


def load_zip(data: bytes, limits: Limits) -> RawPackage:
    hits: list[str] = []
    pkg = RawPackage()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScanBlocked("archive", [f"不是合法的 zip：{exc}"]) from exc

    infos = [i for i in archive.infolist() if not i.is_dir() and not _is_junk(i.filename)]
    if len(infos) > limits.max_files:
        raise ScanBlocked("archive", [f"文件数 {len(infos)} 超过上限 {limits.max_files}"])
    # ★ 按 zip 里**声明**的大小先拦一次，解压炸弹不会走到 read()
    declared = sum(i.file_size for i in infos)
    if declared > limits.max_bytes:
        raise ScanBlocked("archive", [f"解压后 {declared} 字节，超过上限 {limits.max_bytes}"])

    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            hits.append(f"符号链接：{info.filename}")
            continue
        path = _check_path(info.filename, hits)
        if path is None:
            continue
        if info.file_size > limits.max_file_bytes:
            hits.append(f"单文件超限：{path}（{info.file_size} 字节）")
            continue
        body = archive.read(info)
        # 声明值可以撒谎：读出来再核一次
        if len(body) > limits.max_file_bytes:
            hits.append(f"单文件超限：{path}")
            continue
        _check_binary(path, body, hits)
        pkg.files[path] = body
        if mode & 0o111:
            pkg.executable.add(path)

    if pkg.size > limits.max_bytes:
        hits.append(f"总大小 {pkg.size} 超过上限 {limits.max_bytes}")
    if hits:
        raise ScanBlocked("archive", hits)
    return _strip_single_root(pkg)


def load_dir(root: Path, limits: Limits) -> RawPackage:
    hits: list[str] = []
    pkg = RawPackage()
    root = root.resolve()
    candidates = sorted(p for p in root.rglob("*") if not p.is_dir() or p.is_symlink())
    for item in candidates:
        rel = item.relative_to(root).as_posix()
        if _is_junk(rel):
            continue
        if item.is_symlink():
            hits.append(f"符号链接：{rel}")
            continue
        if len(pkg.files) >= limits.max_files:
            raise ScanBlocked("archive", [f"文件数超过上限 {limits.max_files}"])
        size = item.stat().st_size
        if size > limits.max_file_bytes:
            hits.append(f"单文件超限：{rel}（{size} 字节）")
            continue
        body = item.read_bytes()
        _check_binary(rel, body, hits)
        pkg.files[rel] = body
        if item.stat().st_mode & 0o111:
            pkg.executable.add(rel)
    if pkg.size > limits.max_bytes:
        hits.append(f"总大小 {pkg.size} 超过上限 {limits.max_bytes}")
    if hits:
        raise ScanBlocked("archive", hits)
    return pkg
