"""一个人的全部文件（工作空间页面）：{user}/files/ 下一棵普通的目录树。

    list_dir   列一层：递归列举这一层下面的对象，在内存里按一层聚合（文件夹的文件数、大小）
    search     按名称找文件和文件夹，不搜内容
    usage      占用（配额检查用）
    put        上传：经选路（这个人的沙箱在跑时经沙箱写，只有一个写入方）
    mkdir      建文件夹：写一个目录标记对象 {dir}/（s3fs 也这么建目录）
    move       改名 / 移动：逐个对象复制再删（对象存储没有改名），不是原子操作，中途失败重试即可
    delete     删文件、删文件夹：经选路删文件，目录标记对象直接删
    zip        把一个文件夹打包成 zip，流式生成

★ 和会话无关：哪些会话把某个目录当工作目录由服务层查（改名、删除前），这里只管文件。
★ 点开头的路径（.atlas 是会话的内部记录）：显式要求时能列、能读，不能经这里写、改名、删。
★ 路径都过同一道路径关卡（PathGate，根是 files/）：越界的直接拒绝。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from ...domain.workspace_layout import FILES_DIR
from .listing import PreviewObject, WorkspaceListing, _paginate
from .paths import PathEscape
from .workspace import Workspace, is_hidden

__all__ = ["LIST_MAX_KEYS", "UserFiles"]

#: 列一层、搜索、算占用时最多看多少个对象。对象存储没有目录大小，只能遍历；超过就标 truncated
LIST_MAX_KEYS = 20_000
#: 算占用时最多看多少个对象（配额检查）：超过按已看到的算
USAGE_MAX_KEYS = 200_000
_DELETE_BATCH = 1000
_ZIP_CHUNK = 64 * 1024


class UserFiles:
    """fs：根是 files/ 的工作区（make_workspace(..., workspace_dir="")）。"""

    def __init__(self, fs: Workspace) -> None:
        if fs.layout.workspace != FILES_DIR:
            raise ValueError("UserFiles 的根必须是 files/")
        self._fs = fs
        self._listing = WorkspaceListing(fs)
        self._s3 = fs._s3
        self._bucket = fs._bucket
        #: {user}/files/
        self._prefix = fs.layout.key(f"{FILES_DIR}/")

    # ------------------------------------------------------------------ 路径

    def rel(self, path: str) -> str:
        """界面给的路径（相对 files/）→ 规范形式（不带首尾 /）；空串是根。越界抛 PathEscape。"""
        p = path.strip().strip("/")
        if not p:
            return ""
        fp = self._fs.gate.resolve_prefix(f"{self._fs.workdir}/{p}/")
        return fp.rel.removeprefix(f"{FILES_DIR}/").rstrip("/")

    def _writable(self, path: str, action: str) -> str:
        rel = self.rel(path)
        if not rel:
            raise PathEscape(f"不能{action}工作空间的根目录")
        if is_hidden(rel):
            raise PathEscape(f"{rel!r} 是会话的内部记录，不能{action}")
        return rel

    def _key(self, rel: str) -> str:
        return self._prefix + rel

    def _objects(self, rel: str, *, limit: int) -> Iterator[dict[str, Any]]:
        """rel 下面的全部对象（递归），最多 limit 个；多出来的那一个用来判断 truncated。"""
        base = self._prefix + (f"{rel}/" if rel else "")
        n = 0
        for page in _paginate(self._s3, self._bucket, base):
            for item in page.get("Contents", ()):
                n += 1
                if n > limit:
                    yield {"__truncated__": True}
                    return
                yield item

    def is_file(self, path: str) -> bool:
        rel = self.rel(path)
        if not rel:
            return False
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._key(rel))
        except Exception:
            return False
        return True

    def is_dir(self, path: str) -> bool:
        """有东西在它下面（含目录标记对象）。根目录永远算。"""
        rel = self.rel(path)
        if not rel:
            return True
        page = self._s3.list_objects_v2(Bucket=self._bucket, Prefix=f"{self._key(rel)}/", MaxKeys=1)
        return bool(page.get("KeyCount") or page.get("Contents"))

    # ------------------------------------------------------------------ 读

    def list_dir(self, path: str, *, hidden: bool = False) -> dict[str, Any]:
        """列一层：{dir, dirs: [{name, path, files, size, modified_at}], files, truncated}。"""
        rel = self.rel(path)
        base_len = len(self._prefix) + (len(rel) + 1 if rel else 0)
        dirs: dict[str, dict[str, Any]] = {}
        files: list[dict[str, Any]] = []
        truncated = False
        for item in self._objects(rel, limit=LIST_MAX_KEYS):
            if item.get("__truncated__"):
                truncated = True
                break
            rest = item["Key"][base_len:]
            if not rest or (not hidden and is_hidden(rest)):
                continue
            head, sep, tail = rest.partition("/")
            if not sep:
                files.append(_file_out(_join(rel, head), item))
                continue
            d = dirs.setdefault(
                head,
                {
                    "name": head,
                    "path": _join(rel, head),
                    "files": 0,
                    "size": 0,
                    "modified_at": None,
                },
            )
            if tail and not tail.endswith("/"):  # 目录标记对象不是文件
                d["files"] += 1
                d["size"] += int(item.get("Size", 0))
                d["modified_at"] = _latest(d["modified_at"], item.get("LastModified"))
        for d in dirs.values():
            d["modified_at"] = d["modified_at"].isoformat() if d["modified_at"] else None
        return {
            "dir": rel,
            "dirs": sorted(dirs.values(), key=lambda d: d["name"]),
            "files": sorted(files, key=lambda f: f["name"]),
            "truncated": truncated,
        }

    def search(self, q: str, *, hidden: bool = False, limit: int = 200) -> dict[str, Any]:
        """按名称（不分大小写）找文件和文件夹：{dirs, files, truncated}。"""
        needle = q.strip().lower()
        found_dirs: dict[str, dict[str, Any]] = {}
        files: list[dict[str, Any]] = []
        truncated = False
        for item in self._objects("", limit=LIST_MAX_KEYS):
            if item.get("__truncated__"):
                truncated = True
                break
            rel = item["Key"][len(self._prefix) :]
            if not rel or (not hidden and is_hidden(rel)):
                continue
            parts = rel.rstrip("/").split("/")
            for i, seg in enumerate(parts[:-1] if not rel.endswith("/") else parts):
                if needle in seg.lower():
                    p = "/".join(parts[: i + 1])
                    found_dirs.setdefault(p, {"name": seg, "path": p})
            if not rel.endswith("/") and needle in parts[-1].lower():
                files.append(_file_out(rel, item))
        if len(files) + len(found_dirs) > limit:
            truncated = True
        dirs = sorted(found_dirs.values(), key=lambda d: d["path"])[:limit]
        files.sort(key=lambda f: f["modified_at"] or "", reverse=True)
        return {"dirs": dirs, "files": files[: max(limit - len(dirs), 0)], "truncated": truncated}

    def usage(self) -> tuple[int, int]:
        """→ (字节数, 文件数)：files/ 下的全部对象，含隐藏的。"""
        size = count = 0
        for item in self._objects("", limit=USAGE_MAX_KEYS):
            if item.get("__truncated__"):
                break
            if item["Key"].endswith("/"):
                continue
            size += int(item.get("Size", 0))
            count += 1
        return size, count

    def open(self, path: str) -> tuple[Any, int]:
        return self._listing.open(self.rel(path))

    def fetch(
        self, path: str, *, byte_range: str | None = None, if_none_match: str | None = None
    ) -> PreviewObject:
        return self._listing.fetch(path, byte_range=byte_range, if_none_match=if_none_match)

    def zip(self, path: str) -> Iterator[bytes]:
        """把一个文件夹（不含隐藏文件）打包成 zip，边读边吐。根目录也可以。"""
        rel = self.rel(path)
        base_len = len(self._prefix) + (len(rel) + 1 if rel else 0)
        keys = [
            item["Key"]
            for item in self._objects(rel, limit=LIST_MAX_KEYS)
            if not item.get("__truncated__")
            and not item["Key"].endswith("/")
            and not is_hidden(item["Key"][base_len:])
        ]
        sink = _Sink()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for key in keys:
                body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"]
                with zf.open(key[base_len:], "w", force_zip64=True) as dst:
                    for chunk in body.iter_chunks(_ZIP_CHUNK):
                        dst.write(chunk)
                        yield from sink.drain()
                yield from sink.drain()
        yield from sink.drain()

    # ------------------------------------------------------------------ 写

    async def put(self, path: str, data: bytes) -> int:
        rel = self._writable(path, "上传到")
        if path.endswith("/"):
            raise PathEscape(f"要给出文件名：{path!r}")
        if self.is_dir(rel):
            raise FileExistsError(f"已经有同名的文件夹：{rel}")
        return await self._listing.put(rel, data)

    def mkdir(self, path: str) -> str:
        rel = self._writable(path, "新建")
        if self.is_file(rel):
            raise FileExistsError(f"已经有同名的文件：{rel}")
        self._s3.put_object(Bucket=self._bucket, Key=f"{self._key(rel)}/", Body=b"")
        return rel

    def move(self, src: str, dst: str) -> str:
        """改名 / 移动一个文件或文件夹 → 新路径。目标已存在就拒绝（不覆盖）。"""
        s, d = self._writable(src, "移动"), self._writable(dst, "移动到")
        if s == d:
            return d
        if d.startswith(f"{s}/"):
            raise PathEscape("不能把文件夹移动到它自己里面")
        if self.is_file(d) or self.is_dir(d):
            raise FileExistsError(f"已经有同名的文件或文件夹：{d}")
        if self.is_file(s):
            self._copy(self._key(s), self._key(d))
            self._s3.delete_object(Bucket=self._bucket, Key=self._key(s))
            return d
        keys = [item["Key"] for item in self._objects(s, limit=10**9)]
        if not keys:
            raise FileNotFoundError(s)
        src_base, dst_base = f"{self._key(s)}/", f"{self._key(d)}/"
        for key in keys:
            self._copy(key, dst_base + key[len(src_base) :])
        self._remove(keys)
        return d

    async def delete(self, path: str) -> None:
        """删一个文件或文件夹。文件经选路删（沙箱在跑时经沙箱）；剩下的目录标记对象直接删。"""
        rel = self._writable(path, "删除")
        if self.is_file(rel):
            result = await self._fs.adelete(f"{self._fs.workdir}/{rel}")
            if result.error:
                raise OSError(result.error)
            return
        if not self.is_dir(rel):
            raise FileNotFoundError(rel)
        result = await self._fs.adelete(f"{self._fs.workdir}/{rel}/", recursive=True)
        if result.error and not result.error.startswith("Nothing to delete"):
            raise OSError(result.error)
        self._remove([item["Key"] for item in self._objects(rel, limit=10**9)])

    def _copy(self, src_key: str, dst_key: str) -> None:
        self._s3.copy_object(
            Bucket=self._bucket, Key=dst_key, CopySource={"Bucket": self._bucket, "Key": src_key}
        )

    def _remove(self, keys: list[str]) -> None:
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i : i + _DELETE_BATCH]
            self._s3.delete_objects(
                Bucket=self._bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )


class _Sink(io.RawIOBase):
    """zip 写进来的字节先攒着，生成器每写一块就取走（不能 seek：zipfile 改用数据描述符）。"""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        self._chunks.append(bytes(b))
        return len(b)

    def drain(self) -> Iterator[bytes]:
        chunks, self._chunks = self._chunks, []
        yield from chunks


def _join(a: str, b: str) -> str:
    return f"{a}/{b}" if a else b


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _file_out(rel: str, item: dict[str, Any]) -> dict[str, Any]:
    modified = item.get("LastModified")
    return {
        "name": rel.rsplit("/", 1)[-1],
        "path": rel,
        "size": int(item.get("Size", 0)),
        "modified_at": modified.isoformat() if modified else None,
        "etag": str(item.get("ETag", "")).strip('"') or None,
    }
