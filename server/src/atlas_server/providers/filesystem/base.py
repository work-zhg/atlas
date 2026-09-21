"""FilesystemProtocol 的协议再导出与离线 fake。

`MemoryFilesystem` 存在的唯一理由是**离线测试**：让 `make test-engine` 在
只有 Python 的机器上跑通，不必先起一个对象存储。

★ 它<b>不是</b>生产兜底。删掉 StateBackend 时的判断同样适用于这里：
  没配对象存储就是**没有文件能力**，不该悄悄给模型一个 run 结束即弃的
  假工作区 —— 模型不会收到任何提示，只会发现自己写过的东西不见了。
  所以 `HookAssembly` 永远不会构造它，只有测试会。
"""

from __future__ import annotations

import fnmatch
import posixpath

from atlas_engine.contracts import (
    EDIT_MAX_BYTES,
    SEARCH_MAX_KEYS,
    DeleteResult,
    EditResult,
    FilesystemProtocol,
    ReadResult,
    SearchResult,
    WriteResult,
    create_file_data,
    slice_read_response,
)

__all__ = ["FilesystemProtocol", "MemoryFilesystem"]


class MemoryFilesystem:
    """内存实现，语义与 `OssFilesystem` 对齐。

    对齐的点逐条都有测试（tests/test_filesystem_parity.py）：路径归一、
    search 的一层/递归语义、edit 的歧义拒绝、delete 的递归与空前缀报错。
    不对齐的话测试会在一个和生产不同的语义上变绿。
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    # ------------------------------------------------------------------ 路径

    def _norm(self, path: str) -> str:
        norm = posixpath.normpath(posixpath.join("/", path))
        if norm in ("/", "."):
            msg = f"路径必须指向工作区内的文件：{path!r}"
            raise ValueError(msg)
        return norm.lstrip("/")

    # ------------------------------------------------------------------ 读

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        key = self._norm(path)
        if key not in self._files:
            return ReadResult(error=f"File '{path}' not found")
        return slice_read_response(create_file_data(self._files[key]), offset, limit)

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(path, offset, limit)

    def search(
        self, prefix: str, *, pattern: str | None = None, max_keys: int = SEARCH_MAX_KEYS
    ) -> SearchResult:
        base = prefix.strip("/")
        base = f"{base}/" if base else ""
        recursive = bool(pattern and "**" in pattern)
        keys: list[str] = []
        prefixes: set[str] = set()
        truncated = False
        for key in sorted(self._files):
            if not key.startswith(base):
                continue
            rel = key[len(base) :]
            if not rel:
                continue
            if not recursive and "/" in rel:
                prefixes.add(f"{base}{rel.split('/', 1)[0]}/")
                continue
            if pattern:
                stem = pattern[3:] if pattern.startswith("**/") else pattern
                if not (
                    fnmatch.fnmatch(posixpath.basename(key), stem) or fnmatch.fnmatch(key, pattern)
                ):
                    continue
            if len(keys) >= max_keys:
                truncated = True
                break
            keys.append(key)
        return SearchResult(keys=keys, common_prefixes=sorted(prefixes), truncated=truncated)

    async def asearch(
        self, prefix: str, *, pattern: str | None = None, max_keys: int = SEARCH_MAX_KEYS
    ) -> SearchResult:
        return self.search(prefix, pattern=pattern, max_keys=max_keys)

    # ------------------------------------------------------------------ 写

    def write(self, path: str, content: str) -> WriteResult:
        self._files[self._norm(path)] = content
        return WriteResult(path=path)

    async def awrite(self, path: str, content: str) -> WriteResult:
        return self.write(path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        key = self._norm(path)
        if key not in self._files:
            return EditResult(error=f"File '{path}' not found")
        content = self._files[key]
        if len(content.encode("utf-8")) > EDIT_MAX_BYTES:
            return EditResult(error=f"File '{path}' is over the edit limit; use execute with sed.")
        count = content.count(old_string)
        if count == 0:
            return EditResult(error=f"String not found in '{path}': {old_string!r}")
        if count > 1 and not replace_all:
            return EditResult(
                error=f"String appears {count} times in '{path}'. Pass replace_all=true."
            )
        self._files[key] = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        return EditResult(path=path, occurrences=count if replace_all else 1)

    async def aedit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        return self.edit(path, old_string, new_string, replace_all=replace_all)

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        if not recursive:
            key = self._norm(path)
            if key not in self._files:
                return DeleteResult(error=f"File '{path}' not found")
            del self._files[key]
            return DeleteResult(path=path)
        base = self._norm(path).rstrip("/") + "/"
        victims = [k for k in self._files if k.startswith(base)]
        if not victims:
            return DeleteResult(error=f"Nothing to delete under '{path}'")
        for k in victims:
            del self._files[k]
        return DeleteResult(path=path)

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        return self.delete(path, recursive=recursive)
