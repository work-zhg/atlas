"""OssFilesystem —— FilesystemProtocol 的对象存储实现。

用 S3 协议访问：阿里云 OSS 的 S3 兼容端点、MinIO、AWS S3 三者同一份代码。
本地开发用 MinIO，生产用 OSS，测试用 moto 的内存桶 —— 不需要为每种环境
再写一个实现（`FilesystemProtocol 只保留一个 OSS 实现`）。

同步核心 + `asyncio.to_thread` 包出异步版：boto3 是同步的，而 agent 图跑在
asyncio 上。不引 aioboto3 是因为它多一层依赖却只解决同一个问题，而对象
存储调用的耗时由网络决定，线程切换的开销可忽略。

★ 会话隔离全靠 `_to_key()`。它是本模块唯一的安全关键点 ——
  写错了等于会话之间可以互相读写。相关断言在
  tests/test_oss_filesystem.py::test_path_escapes_are_rejected。
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from uuid import UUID

__all__ = [
    "READ_MAX_BYTES",
    "SKILLS_MOUNT",
    "WORKSPACE_MOUNT",
    "OssFilesystem",
    "PathEscape",
]

#: 单次 `read` 允许取回的对象大小。对象存储没有按行的服务端分页，
#: 一次 read 必须把对象取回来再切行 —— 没有上限的话，模型对一个 500MB 的
#: 日志调一次 read_file 就会把执行器内存打爆。
#: 超过时返回带替代方案的错误（用 execute 跑 sed/head），不静默截断。
READ_MAX_BYTES = 8 * 1024 * 1024

#: 批量删除单次的 key 上限。S3 的 DeleteObjects 规定 1000，OSS 同。
_DELETE_BATCH = 1000

#: 模型看到的技能挂载点。它不在 workspace 之内 —— `_to_key` 把这个前缀
#: 路由到本会话的 skills 前缀（与 workspace 平级的兄弟目录）。
SKILLS_MOUNT = "/skills"
#: 工作区在容器里的挂载点（与 cluster 模板的 WORKSPACE_MOUNT 一致）。
#:
#: ★ 模型看得见这个路径 —— adapter 的 cwd 就是它，工具回显的也是它 ——
#:   所以模型会很自然地写出 `/workspace/a.md`。不认这个前缀的话，
#:   它会被当成工作区里一个**名叫 workspace 的子目录**，于是对象落在
#:   `<ws>/workspace/a.md`。这个错法是静默的：写入成功、事件照发，
#:   只是文件从此出现在一个谁也不会去看的地方。真跑第一轮就撞上了。
WORKSPACE_MOUNT = "/workspace"


class PathEscape(ValueError):
    """路径试图逃出会话工作区前缀。

    不是"文件不存在"那类可恢复错误 —— 它要么是模型在乱试，要么是调用方
    拼错了路径。两种都该显式失败，而不是悄悄落到别的会话上。
    """


def _under_workspace_mount(norm: str) -> bool:
    return norm == WORKSPACE_MOUNT or norm.startswith(f"{WORKSPACE_MOUNT}/")


class OssFilesystem:
    """一个会话的工作区。

    Args:
        client: boto3 的 S3 client（`boto3.client("s3", endpoint_url=...)`）。
        bucket: 桶名。
        root: 桶内的环境前缀，如 `"prod"`。
        session_id: 会话 id —— 工作区前缀由它派生。
    """

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        user_id: UUID | str,
        thread_id: UUID | str,
        workspace_thread_id: UUID | str | None = None,
    ) -> None:
        """
        Args:
            user_id: 拥有者。进路径 —— 逃逸防护因此同时挡住跨用户。
            thread_id: 本会话。skills 与 system 恒用它。
            workspace_thread_id: 工作区归属的会话。None = 自己（主 agent）；
                子智能体传**父会话**的 id —— 它与主 agent 共享同一份工作区
                （委派常常就是「帮我处理这批文件」，产物要互相看得见）。
        """
        self._s3 = client
        self._bucket = bucket
        base = f"{user_id}/{thread_id}"
        ws_base = f"{user_id}/{workspace_thread_id or thread_id}"

        self._ws = f"{ws_base}/workspace"
        #: 技能副本。★ 与 workspace **平级**，不在它之内 —— 因为 workspace
        #: 是**共享挂载**的：子智能体的 Pod 挂父 agent 的 workspace，技能若
        #: 住在里面就会被一起带上，而它应该有自己的一套。
        #: 平级之后这是结构性的，不是约定：挂载根本带不上。
        self._skills = f"{base}/skills"
        #: 系统区：压缩归档、大结果外置。同样平级 —— 放进工作区的话，
        #: 模型 search("") 会被它们淹没并当成自己的产物。
        self._sys = f"{base}/system"
        #: 技能源仓库（全局只读，配置平面按版本发布）。会话副本从这里拷。
        self._skills_repo = "_skills"

    # ------------------------------------------------------------------ 路径

    def _to_key(self, path: str) -> str:
        """相对路径 → 绝对 object key。

        ★ 规范化必须在拼前缀**之前**做。先拼后归一的话，
          `../../other-thread/x` 会被归一成另一个会话的真实 key ——
          那是一个跨会话的读写漏洞，而且从调用方看毫无异常。
          路径里现在有 user 段，所以这一条同时挡住跨用户。

        ★ `/skills/**` 路由到**自己的** skills 前缀，其余落 workspace。
          两者是平级的兄弟目录，所以这里只是一次前缀选择 —— 不需要
          CompositeBackend 那种通用路由（它已随旧协议一起删除）。
        """
        if "\x00" in path:
            raise PathEscape(f"路径含空字节：{path!r}")
        absolute = path.startswith("/")
        norm = posixpath.normpath(posixpath.join("/", path))
        if norm in ("/", "."):
            raise PathEscape(f"路径必须指向一个文件，而不是工作区本身：{path!r}")
        if norm == SKILLS_MOUNT or norm.startswith(f"{SKILLS_MOUNT}/"):
            return f"{self._skills}{norm[len(SKILLS_MOUNT) :]}"
        if absolute and _under_workspace_mount(norm):
            # ★ 只在**绝对**路径上剥。`/workspace/a.md` 是"挂载点下的 a.md"，
            #   而相对的 `workspace/a.md` 是"工作区里那个叫 workspace 的
            #   子目录下的 a.md" —— 两个不同的意思，而且从原始入参分得出来，
            #   所以不该合并。（skills 那条历史上两种都剥，这里不动它。）
            rest = norm[len(WORKSPACE_MOUNT) :]
            if not rest:
                raise PathEscape(f"路径必须指向一个文件，而不是工作区本身：{path!r}")
            return f"{self._ws}{rest}"
        return f"{self._ws}{norm}"

    def _to_prefix(self, prefix: str) -> tuple[str, str, str]:
        """相对前缀 → (绝对 key 前缀, 所属根, 模型侧根)。

        返回三元组而不是一个字符串：`search` 列举完还要把 key 还原成模型
        看到的路径，而**还原用哪个根取决于路由到了哪个前缀** —— 只回绝对
        前缀的话，skills 下的 key 会被按 workspace 的根去剥，剥不掉。
        """
        if "\x00" in prefix:
            raise PathEscape(f"前缀含空字节：{prefix!r}")
        trailing = prefix.endswith("/") or prefix == ""
        absolute = prefix.startswith("/")
        norm = posixpath.normpath(posixpath.join("/", prefix))

        if absolute and _under_workspace_mount(norm):
            # 与 _to_key 同一条规则 —— 列举和读写必须对同一个前缀说同一件事，
            # 否则会出现"写进去了但 search 看不见"。
            norm = norm[len(WORKSPACE_MOUNT) :]
            if not norm:
                return f"{self._ws}/", self._ws, ""

        if norm == SKILLS_MOUNT or norm.startswith(f"{SKILLS_MOUNT}/"):
            rest = norm[len(SKILLS_MOUNT) :]
            return (
                f"{self._skills}{rest}{'/' if trailing and rest else '/' if not rest else ''}",
                self._skills,
                SKILLS_MOUNT.lstrip("/"),
            )
        if norm in ("/", "."):
            return f"{self._ws}/", self._ws, ""
        return f"{self._ws}{norm}{'/' if trailing else ''}", self._ws, ""

    @staticmethod
    def _to_path(key: str, base: str, shown_root: str) -> str:
        """object key → 模型看到的相对路径。"""
        rel = key.removeprefix(f"{base}/")
        return f"{shown_root}/{rel}" if shown_root else rel

    # ------------------------------------------------------------------ 读

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        key = self._to_key(path)
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return ReadResult(error=f"File '{path}' not found")

        size = int(head.get("ContentLength", 0))
        if size > READ_MAX_BYTES:
            return ReadResult(
                error=(
                    f"File '{path}' is {size} bytes, over the {READ_MAX_BYTES}-byte "
                    f"read limit. Use execute to page through it "
                    f"(e.g. `sed -n '1,200p' {path}`)."
                )
            )

        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError:
            # 二进制对象：中间件按 encoding 决定怎么呈现，这里不猜。
            import base64

            return ReadResult(
                file_data=create_file_data(
                    base64.b64encode(body).decode("ascii"), encoding="base64"
                )
            )
        return slice_read_response(create_file_data(content), offset, limit)

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await asyncio.to_thread(self.read, path, offset, limit)

    def search(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult:
        """前缀列举，可选再按文件名 glob 过滤。

        ★ 不搜索内容 —— 对象存储做不到。工具描述必须写明并指向 execute。

        `pattern` 含 `**` 时递归列举（不带 delimiter），否则只列一层并把
        子目录以 `common_prefixes` 返回 —— 后者是用 delimiter 模拟出来的，
        对象存储没有真正的目录。
        """
        try:
            abs_prefix, base, shown_root = self._to_prefix(prefix)
        except PathEscape as exc:
            return SearchResult(error=str(exc))

        recursive = bool(pattern and "**" in pattern)
        # 显式指定了点开头的前缀就是要看它 —— 此时不再过滤
        include_hidden = _is_hidden(prefix.lstrip("/"))
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": abs_prefix}
        if not recursive:
            kwargs["Delimiter"] = "/"

        keys: list[str] = []
        prefixes: list[str] = []
        truncated = False
        token: str | None = None
        while True:
            if token:
                kwargs["ContinuationToken"] = token
            # 多取一个，用来区分"正好取满"与"还有更多"
            kwargs["MaxKeys"] = min(1000, max_keys - len(keys) + 1)
            page = self._s3.list_objects_v2(**kwargs)

            for item in page.get("Contents", ()):
                rel = self._to_path(item["Key"], base, shown_root)
                # ★ 默认跳过点开头的条目（.skills/ 等）。技能包动辄几十个
                #   文件（ui-ux-pro-max 有 76 个），不过滤的话模型每次
                #   search("") 都会被技能文件淹没，看不见自己的产物。
                #   显式给出前缀（search(".skills/")）时仍然列得出来。
                if not include_hidden and _is_hidden(rel):
                    continue
                if pattern and not _matches(rel, pattern):
                    continue
                if len(keys) >= max_keys:
                    truncated = True
                    break
                keys.append(rel)
            for cp in page.get("CommonPrefixes", ()):
                rel_prefix = self._to_path(cp["Prefix"], base, shown_root)
                if include_hidden or not _is_hidden(rel_prefix):
                    prefixes.append(rel_prefix)

            token = page.get("NextContinuationToken")
            if truncated or not page.get("IsTruncated") or not token:
                break

        return SearchResult(keys=keys, common_prefixes=prefixes, truncated=truncated)

    async def asearch(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult:
        return await asyncio.to_thread(self.search, prefix, pattern=pattern, max_keys=max_keys)

    # ------------------------------------------------------------------ 写

    def write(self, path: str, content: str) -> WriteResult:
        key = self._to_key(path)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=content.encode("utf-8"))
        return WriteResult(path=path)

    async def awrite(self, path: str, content: str) -> WriteResult:
        return await asyncio.to_thread(self.write, path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        """字符串替换 —— read-modify-write。

        ★ 对象存储不能原地改：必须把整个对象取回、替换、整体写回。
          因此有 EDIT_MAX_BYTES 上限，超过时给出可执行的替代方案而不是
          默默把一个大对象拉进内存。
        """
        key = self._to_key(path)
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return EditResult(error=f"File '{path}' not found")

        size = int(head.get("ContentLength", 0))
        if size > EDIT_MAX_BYTES:
            return EditResult(
                error=(
                    f"File '{path}' is {size} bytes, over the {EDIT_MAX_BYTES}-byte "
                    f"edit limit (object storage cannot edit in place — the whole "
                    f"object must be rewritten). Use execute with sed instead."
                )
            )

        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        try:
            content = body.decode("utf-8")
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

        updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=updated.encode("utf-8"))
        return EditResult(path=path, occurrences=count if replace_all else 1)

    async def aedit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        return await asyncio.to_thread(
            self.edit, path, old_string, new_string, replace_all=replace_all
        )

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """删除对象；`recursive` 时先列举前缀再分批删。

        对象存储没有"目录"，删目录 = 删掉该前缀下的全部 key。批量删除单次
        有 1000 个的上限，所以要分批。
        """
        if not recursive:
            key = self._to_key(path)
            try:
                self._s3.head_object(Bucket=self._bucket, Key=key)
            except Exception:
                return DeleteResult(error=f"File '{path}' not found")
            self._s3.delete_object(Bucket=self._bucket, Key=key)
            return DeleteResult(path=path)

        prefix, _base, _shown = self._to_prefix(path if path.endswith("/") else f"{path}/")
        victims = [
            item["Key"]
            for page in _paginate(self._s3, self._bucket, prefix)
            for item in page.get("Contents", ())
        ]
        if not victims:
            return DeleteResult(error=f"Nothing to delete under '{path}'")
        for i in range(0, len(victims), _DELETE_BATCH):
            batch = victims[i : i + _DELETE_BATCH]
            self._s3.delete_objects(
                Bucket=self._bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )
        return DeleteResult(path=path)

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        return await asyncio.to_thread(self.delete, path, recursive=recursive)


def _is_hidden(rel_path: str) -> bool:
    """路径的任一段以点开头。

    `.skills/x/SKILL.md` 与 `.atlas/out/xxx` 都命中 —— 隐藏是按段判断的，
    否则 `reports/.draft.md` 这种会漏网。
    """
    return any(seg.startswith(".") for seg in rel_path.split("/") if seg)


def _matches(rel_path: str, pattern: str) -> bool:
    """文件名 glob 匹配。

    `**/x` 形态跨目录匹配（与 pathlib 的 glob 语义一致），其余按
    fnmatch 对整条相对路径匹配。
    """
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(posixpath.basename(rel_path), pattern[3:]) or fnmatch.fnmatch(
            rel_path, pattern
        )
    return fnmatch.fnmatch(rel_path, pattern)


def _paginate(s3: Any, bucket: str, prefix: str) -> Any:
    """遍历一个前缀下的全部对象（无 delimiter，递归）。"""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        yield page
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return
