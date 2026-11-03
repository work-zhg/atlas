"""一个 run 的文件能力（code-workspace.html · Workspace），实现引擎的 FilesystemProtocol。

每次操作先过路径关卡（PathGate），再由选路（StoreRouter）决定这一次走哪个底层；上层语义（切行、
edit、隐藏文件、截断、上限）只写一份，对两种底层都成立。

    同步方法     只直连对象存储（给图外的调用方：测试、脚本）
    异步方法     工作区区按选路：原生沙箱 READY 时经沙箱，否则直连；技能区、系统区始终直连

用 S3 协议访问：阿里云 OSS 的 S3 兼容端点、MinIO、AWS S3 三者同一份代码；本地开发用 MinIO，生产用
OSS，测试用 moto 的内存桶。boto3 是同步的：同步核心 + asyncio.to_thread。
"""

from __future__ import annotations

import base64
import fnmatch
import posixpath
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

from atlas_engine.contracts import (
    EDIT_MAX_BYTES,
    SEARCH_MAX_KEYS,
    DeleteResult,
    EditResult,
    ReadResult,
    SearchResult,
    WriteResult,
    create_file_data,
    slice_read_response,
)

from ...domain.workspace_layout import WorkspaceLayout
from .paths import FilePath, PathEscape, PathGate
from .router import StoreRouter, WorkspaceRouting
from .stores import FileStore, ObjectStoreFiles

__all__ = ["READ_MAX_BYTES", "Workspace", "is_hidden"]

T = TypeVar("T")

#: 单次 read 允许取回的对象大小。对象存储没有按行的服务端分页，一次 read 必须把对象取回来再切行 ——
#: 没有上限的话，模型对一个 500MB 的日志调一次 read_file 就会把执行器内存打爆。
#: 超过时返回带替代方案的错误（用 execute 跑 sed/head），不静默截断。
READ_MAX_BYTES = 8 * 1024 * 1024


class Workspace:
    """一个会话的文件。"""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        user_id: UUID | str,
        thread_id: UUID | str,
        workspace_thread_id: UUID | str | None = None,
        routing: WorkspaceRouting | None = None,
    ) -> None:
        """
        Args:
            user_id: 拥有者。进路径 —— 逃逸防护因此同时挡住跨用户。
            thread_id: 本会话。技能区与系统区恒用它。
            workspace_thread_id: 工作区归属的会话。None = 自己（主 agent）；
                子智能体传**父会话**的 id
                —— 它与主 agent 共享同一份工作区（委派常常就是「帮我处理这批文件」）。
            routing: 工作区的沙箱路由。None = 永远直连对象存储（没开沙箱的部署）。
        """
        self.layout = WorkspaceLayout.of(user_id, thread_id, workspace_thread_id)
        self.gate = PathGate()
        self.objects = ObjectStoreFiles(client, bucket, self.layout)
        self._router = StoreRouter(self.objects, routing)
        self._s3 = client
        self._bucket = bucket

    def key_of(self, path: str) -> str:
        """模型 / 界面给的路径 → object key（过路径关卡；越界抛 PathEscape）。"""
        return self.objects.key(self.gate.resolve(path))

    async def _routed(
        self,
        fp: FilePath | None,
        fn: Callable[[FileStore], T],
        *,
        refuse: Callable[[str], T] | None,
    ) -> T:
        return await self._router.run(
            fn, in_sandbox=fp is not None and fp.in_sandbox(), refuse=refuse
        )

    # ------------------------------------------------------------------ 读

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return ReadResult(error=str(exc))
        return self._read(fp, path, offset, limit, self.objects)

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        # ★ 越界 / 指向工作区根返回错误结果，不抛：抛出去会打断整个 run（模型读个目录就失败）
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return ReadResult(error=str(exc))
        return await self._routed(
            fp, lambda store: self._read(fp, path, offset, limit, store), refuse=None
        )

    @staticmethod
    def _read(fp: FilePath, path: str, offset: int, limit: int, store: FileStore) -> ReadResult:
        size = store.size(fp)
        if size is None:
            return ReadResult(error=f"File '{path}' not found")
        if size > READ_MAX_BYTES:
            return ReadResult(
                error=(
                    f"File '{path}' is {size} bytes, over the {READ_MAX_BYTES}-byte "
                    f"read limit. Use execute to page through it "
                    f"(e.g. `sed -n '1,200p' {path}`)."
                )
            )
        body = store.get(fp)
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError:
            # 二进制对象：中间件按 encoding 决定怎么呈现，这里不猜
            return ReadResult(
                file_data=create_file_data(
                    base64.b64encode(body).decode("ascii"), encoding="base64"
                )
            )
        return slice_read_response(create_file_data(content), offset, limit)

    def search(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult:
        """前缀列举，可选再按文件名 glob 过滤。

        ★ 不搜索内容 —— 对象存储做不到。工具描述必须写明并指向 execute。

        pattern 含 ** 时递归列举（不带 delimiter），否则只列一层并把子目录以 common_prefixes
        返回 —— 后者是用 delimiter 模拟出来的，对象存储没有真正的目录。
        """
        try:
            fp = self.gate.resolve_prefix(prefix)
        except PathEscape as exc:
            return SearchResult(error=str(exc))
        return self._search(fp, prefix, pattern, max_keys, self.objects)

    async def asearch(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult:
        try:
            fp = self.gate.resolve_prefix(prefix)
        except PathEscape as exc:
            return SearchResult(error=str(exc))
        return await self._routed(
            fp, lambda store: self._search(fp, prefix, pattern, max_keys, store), refuse=None
        )

    def _search(
        self, fp: FilePath, prefix: str, pattern: str | None, max_keys: int, store: FileStore
    ) -> SearchResult:
        recursive = bool(pattern and "**" in pattern)
        # 显式指定了点开头的前缀就是要看它 —— 此时不再过滤
        include_hidden = is_hidden(prefix.lstrip("/"))
        keys: list[str] = []
        prefixes: list[str] = []
        truncated = False
        for page_files, page_dirs in store.pages(fp, recursive=recursive):
            for rel in page_files:
                if rel.endswith("/"):
                    continue  # 目录标记对象（s3fs 建目录时会写一个）—— 不是文件
                shown = self.gate.shown(FilePath(fp.area, rel))
                # ★ 默认跳过点开头的条目。不过滤的话模型每次 search("") 都会被它们淹没，
                #   看不见自己的产物。显式给出前缀（search(".x/")）时仍然列得出来
                if not include_hidden and is_hidden(shown):
                    continue
                if pattern and not _matches(shown, pattern):
                    continue
                if len(keys) >= max_keys:
                    truncated = True
                    break
                keys.append(shown)
            for rel in page_dirs:
                shown = self.gate.shown(FilePath(fp.area, rel))
                if include_hidden or not is_hidden(shown):
                    prefixes.append(shown)
            if truncated:
                break
        return SearchResult(keys=keys, common_prefixes=prefixes, truncated=truncated)

    # ------------------------------------------------------------------ 写

    def write(self, path: str, content: str) -> WriteResult:
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return WriteResult(error=str(exc))
        return self._write(fp, path, content.encode("utf-8"), self.objects)

    async def awrite(self, path: str, content: str) -> WriteResult:
        return await self.aput(path, content.encode("utf-8"))

    async def aput(self, path: str, data: bytes) -> WriteResult:
        """写一个文件（字节）。界面上传也走这里：和模型的写走同一个选路、同一个写入方。"""
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return WriteResult(error=str(exc))
        return await self._routed(
            fp,
            lambda store: self._write(fp, path, data, store),
            refuse=lambda msg: WriteResult(error=msg),
        )

    @staticmethod
    def _write(fp: FilePath, path: str, data: bytes, store: FileStore) -> WriteResult:
        store.put(fp, data)
        return WriteResult(path=path)

    def edit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        """字符串替换 —— read-modify-write。

        ★ 对象存储不能原地改：必须把整个对象取回、替换、整体写回。因此有 EDIT_MAX_BYTES 上限，
          超过时给出可执行的替代方案而不是默默把一个大对象拉进内存。
        """
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return EditResult(error=str(exc))
        return self._edit(fp, path, old_string, new_string, replace_all, self.objects)

    async def aedit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        try:
            fp = self.gate.resolve(path)
        except PathEscape as exc:
            return EditResult(error=str(exc))
        return await self._routed(
            fp,
            lambda store: self._edit(fp, path, old_string, new_string, replace_all, store),
            refuse=lambda msg: EditResult(error=msg),
        )

    @staticmethod
    def _edit(
        fp: FilePath,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
        store: FileStore,
    ) -> EditResult:
        size = store.size(fp)
        if size is None:
            return EditResult(error=f"File '{path}' not found")
        if size > EDIT_MAX_BYTES:
            return EditResult(
                error=(
                    f"File '{path}' is {size} bytes, over the {EDIT_MAX_BYTES}-byte "
                    f"edit limit (object storage cannot edit in place — the whole "
                    f"object must be rewritten). Use execute with sed instead."
                )
            )
        try:
            content = store.get(fp).decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(error=f"File '{path}' is not utf-8 text; cannot edit")

        count = content.count(old_string)
        if count == 0:
            return EditResult(error=f"String not found in '{path}': {old_string!r}")
        if count > 1 and not replace_all:
            return EditResult(
                error=(
                    f"String appears {count} times in '{path}'. "
                    f"Pass replace_all=true, or give a longer unique string."
                )
            )
        updated = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )
        store.put(fp, updated.encode("utf-8"))
        return EditResult(path=path, occurrences=count if replace_all else 1)

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """删除对象；recursive 时先列举前缀再分批删（对象存储没有「目录」）。"""
        try:
            fp = self._delete_target(path, recursive)
        except PathEscape as exc:
            return DeleteResult(error=str(exc))
        return self._delete(fp, path, recursive, self.objects)

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        try:
            fp = self._delete_target(path, recursive)
        except PathEscape as exc:
            return DeleteResult(error=str(exc))
        return await self._routed(
            fp,
            lambda store: self._delete(fp, path, recursive, store),
            refuse=lambda msg: DeleteResult(error=msg),
        )

    def _delete_target(self, path: str, recursive: bool) -> FilePath:
        if not recursive:
            return self.gate.resolve(path)
        return self.gate.resolve_prefix(path if path.endswith("/") else f"{path}/")

    @staticmethod
    def _delete(fp: FilePath, path: str, recursive: bool, store: FileStore) -> DeleteResult:
        if not recursive:
            if store.size(fp) is None:
                return DeleteResult(error=f"File '{path}' not found")
            store.remove([fp])
            return DeleteResult(path=path)
        victims = [
            FilePath(fp.area, rel) for files, _ in store.pages(fp, recursive=True) for rel in files
        ]
        if not victims:
            return DeleteResult(error=f"Nothing to delete under '{path}'")
        store.remove(victims)
        return DeleteResult(path=path)


def is_hidden(rel_path: str) -> bool:
    """路径的任一段以点开头（按段判断，否则 reports/.draft.md 这种会漏网）。"""
    return any(seg.startswith(".") for seg in rel_path.split("/") if seg)


def _matches(rel_path: str, pattern: str) -> bool:
    """文件名 glob 匹配。**/x 形态跨目录匹配（与 pathlib 的 glob 语义一致），其余按 fnmatch 对整条
    相对路径匹配。"""
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(posixpath.basename(rel_path), pattern[3:]) or fnmatch.fnmatch(
            rel_path, pattern
        )
    return fnmatch.fnmatch(rel_path, pattern)
