"""FilesystemProtocol —— 会话工作区的文件访问契约。

★ 定义住在 contracts（架构的最底层），不在 kernel：协议是 server 侧实现的
  目标契约（OssFilesystem），也是 kernel 中间件的消费对象 —— 两边都认的
  东西必须住在两边都够得着、且**谁都不拥有**的地方。kernel 反向依赖本层。

与 `SandboxProtocol` **并列**，不是继承关系。旧的
`SandboxBackendProtocol(BackendProtocol)` 把「能跑命令」建模成「文件系统的
一种」，于是沙箱被迫提供全套文件操作（`BaseSandbox` 里 13 个方法都是用
`execute()` 现跑一段脚本实现的）。拆开之后两件事各归各位：

    FilesystemProtocol   文件存在哪、怎么读写
    SandboxProtocol      命令在哪执行

文末的几个函数（create_file_data / slice_read_response …）是读窗口分页
语义的**参考实现** —— `ReadResult.__post_init__` 的校验与 slice 的产出是
同一份语义的两半，实现方（server 的 OssFilesystem、kernel 的中间件）共用
它们以保持行为一致。

本协议只声明**对象存储能做到的事**：

  · 没有 `grep` —— OSS 不支持内容检索。按内容查找归 `execute`。
  · 没有 `ls` / `glob` —— 统一成 `search`（前缀 + 可选文件名 glob），
    对应 OSS 的 ListObjects + 客户端过滤。
  · `edit` 是 read-modify-write —— 对象存储没有原地修改，实现必须把整个
    对象取回、改完再整体写回，因此要有大小上限（见 `EDIT_MAX_BYTES`）。

路径一律相对会话工作区根（模型看到 `reports/q3.md`，实现层拼上
`sessions/{id}/workspace/` 前缀）。**路径规范化必须在拼前缀之前做** ——
先拼后归一的话 `../../other-session/x` 会归一成另一个会话的真实 key。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from typing_extensions import NotRequired, TypedDict

__all__ = [
    "EDIT_MAX_BYTES",
    "SEARCH_MAX_KEYS",
    "DeleteResult",
    "EditResult",
    "FileData",
    "FilesystemProtocol",
    "ReadResult",
    "SearchResult",
    "WriteResult",
    "create_file_data",
    "file_data_to_string",
    "normalize_read_bounds",
    "slice_read_response",
]

logger = logging.getLogger(__name__)

class FileData(TypedDict):
    """Data structure for storing file contents with metadata."""

    content: str
    """File content as a plain string (utf-8 text or base64-encoded binary)."""

    encoding: str
    """Content encoding: `"utf-8"` for text, `"base64"` for binary."""

    created_at: NotRequired[str]
    """ISO 8601 timestamp of file creation."""

    modified_at: NotRequired[str]
    """ISO 8601 timestamp of last modification."""

@dataclass
class ReadResult:
    """Result from backend read operations."""

    error: str | None = None
    """Error message on failure, `None` on success."""

    file_data: FileData | None = None
    """File data on success, `None` on failure."""

    total_lines: int | None = None
    """Total number of source lines when the backend can determine it."""

    start_line: int | None = None
    """1-indexed first source line returned in `file_data`."""

    end_line: int | None = None
    """1-indexed last source line returned in `file_data`."""

    next_offset: int | None = None
    """0-indexed offset for the next unread source line."""

    no_lines_requested: bool = False
    """The read asked for zero lines and the file was never inspected.

    Set by backends when a non-positive `limit` short-circuits the read, so
    the middleware can tell a never-inspected window apart from a file that
    was inspected and is genuinely empty — both otherwise arrive as empty
    content with no pagination metadata.
    """

    def __post_init__(self) -> None:
        """Reject malformed pagination-field combinations at construction.

        The window fields are not independent: `start_line`/`end_line` are a
        pair, and neither `next_offset` nor `total_lines` describes anything
        without the window it refers to. Beyond co-presence, the values must
        agree numerically: a window runs forward (`1 <= start_line <=
        end_line`), the file is at least as long as the window
        (`total_lines >= end_line`), and the resume point is the 0-indexed line
        immediately after the last one shown (`next_offset == end_line`, since
        `end_line` is 1-indexed). Fail loudly here to keep a backend from
        emitting a `next_offset` that would silently skip unshown source lines
        once it reaches the middleware.
        """
        if (self.start_line is None) != (self.end_line is None):
            msg = "ReadResult.start_line and end_line must be set together or both left unset"
            raise ValueError(msg)
        if self.no_lines_requested and (
            self.error is not None or self.start_line is not None or self.next_offset is not None or self.total_lines is not None
        ):
            msg = "ReadResult.no_lines_requested describes an uninspected window; it cannot be combined with error or pagination fields"
            raise ValueError(msg)
        if self.next_offset is not None and self.start_line is None:
            msg = "ReadResult.next_offset requires start_line and end_line to be set"
            raise ValueError(msg)
        if self.total_lines is not None and self.start_line is None:
            msg = "ReadResult.total_lines requires start_line and end_line to be set"
            raise ValueError(msg)

        # Numeric consistency of a present window. `start_line`/`end_line` are
        # bound together above, so testing `start_line` covers both.
        if self.start_line is not None and self.end_line is not None:
            if self.start_line < 1 or self.end_line < self.start_line:
                msg = f"ReadResult window must satisfy 1 <= start_line <= end_line, got start_line={self.start_line}, end_line={self.end_line}"
                raise ValueError(msg)
            if self.total_lines is not None and self.total_lines < self.end_line:
                msg = f"ReadResult.total_lines ({self.total_lines}) cannot be less than end_line ({self.end_line})"
                raise ValueError(msg)
            if self.next_offset is not None and self.next_offset != self.end_line:
                msg = f"ReadResult.next_offset ({self.next_offset}) must equal end_line ({self.end_line}), the 0-indexed line after the last shown"
                raise ValueError(msg)

@dataclass
class WriteResult:
    """Result from backend `write` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        path: Absolute path of written file, `None` on failure.

    Examples:
        >>> WriteResult(path="/f.txt")
        >>> WriteResult(error="File exists")
    """

    error: str | None = None
    path: str | None = None

@dataclass
class EditResult:
    """Result from backend `edit` operations.

    Attributes:
        error: Error message on failure, `None` on success.
        path: Absolute path of edited file, `None` on failure.
        occurrences: Number of replacements made, `None` on failure.

    Examples:
        >>> EditResult(path="/f.txt", occurrences=1)
        >>> EditResult(error="File not found")
    """

    error: str | None = None
    path: str | None = None
    occurrences: int | None = None

@dataclass
class DeleteResult:
    """Result from backend delete operations.

    Attributes:
        error: Error message on failure, None on success.
        path: Absolute path of the deleted file, None on failure.

    Examples:
        >>> DeleteResult(path="/f.txt")
        >>> DeleteResult(error="File not found")
    """

    error: str | None = None
    path: str | None = None


#: `edit` 允许的对象大小上限。超过时明确报错并提示改用 `execute` 跑 `sed`，
#: 而不是默默把一个几百 MB 的对象拉进执行器内存 —— 对象存储不能原地改，
#: 一次 `edit` 必然要取回全文。
EDIT_MAX_BYTES = 2 * 1024 * 1024

#: 单次 `search` 返回的 key 上限。大前缀下递归列举会拉回极多 key，
#: 既占上下文也拖慢响应。撞到上限时 `SearchResult.truncated` 为真，
#: 调用方要把「请缩小前缀范围」这句话给到模型，而不是只标个布尔。
SEARCH_MAX_KEYS = 1000


@dataclass
class SearchResult:
    """`search` 的结果。

    Attributes:
        error: 失败时的错误消息，成功为 `None`。
        keys: 命中的文件路径（已剥掉会话前缀，即模型看到的形态）。
        common_prefixes: 以 delimiter 分组出的「子目录」名。对象存储没有
            真正的目录，这是用 `ListObjects(delimiter="/")` 模拟出来的。
        truncated: 是否撞到 `max_keys` 上限。
    """

    error: str | None = None
    keys: list[str] = field(default_factory=list)
    common_prefixes: list[str] = field(default_factory=list)
    truncated: bool = False


@runtime_checkable
class FilesystemProtocol(Protocol):
    """会话工作区的文件访问。

    每个方法都有一个 `a` 前缀的异步版本 —— agent 图跑在 asyncio 上，
    同步版本只为少数同步调用路径保留。
    """

    # ------------------------------------------------------------------ 读

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """按行分页读取。

        Args:
            path: 相对工作区根的路径。
            offset: 0-indexed 起始行。
            limit: 最多返回多少源码行。
        """
        ...

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> ReadResult: ...

    def search(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult:
        """按前缀列举，可选再按文件名 glob 过滤。

        ★ 不搜索文件内容。工具描述必须写明这一点并指向 `execute` 跑 `grep`，
          否则模型会反复用它找内容、拿到空结果、然后据此下「没找到」的结论。

        Args:
            prefix: 路径前缀。空串表示工作区根。
            pattern: 可选的文件名 glob，在列举结果上做客户端过滤。
                `**/` 形态表示递归（列举时不带 delimiter）。
            max_keys: 返回上限。
        """
        ...

    async def asearch(
        self,
        prefix: str,
        *,
        pattern: str | None = None,
        max_keys: int = SEARCH_MAX_KEYS,
    ) -> SearchResult: ...

    # ------------------------------------------------------------------ 写

    def write(self, path: str, content: str) -> WriteResult:
        """整体写入。对象存储没有追加语义，同路径即覆盖。"""
        ...

    async def awrite(self, path: str, content: str) -> WriteResult: ...

    def edit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult:
        """字符串替换。

        ★ 实现是 read-modify-write：取回整个对象、替换、整体写回。
          超过 `EDIT_MAX_BYTES` 必须返回带替代方案的错误，不要静默吞内存。
        """
        ...

    async def aedit(
        self, path: str, old_string: str, new_string: str, *, replace_all: bool = False
    ) -> EditResult: ...

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """删除对象；`recursive` 时先列举前缀再批量删。

        多数对象存储的批量删除单次有上限（常见 1000），实现要分批。
        """
        ...

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult: ...


# ────────────────────────────── 读窗口语义的参考实现 ──────────────────────────────


def _normalize_content(file_data: FileData) -> str:
    """Normalize current and legacy file data content to a plain string.

    Args:
        file_data: `FileData` dict with `content` key.

    Returns:
        Content as a single string.

    Raises:
        TypeError: If content is neither a string nor a legacy list of strings.
    """
    content: object = file_data["content"]
    if isinstance(content, list) and all(isinstance(line, str) for line in content):
        return "\n".join(content)
    if not isinstance(content, str):
        msg = f"File content must be a string or a legacy list of strings, got {type(content).__name__}."
        raise TypeError(msg)
    return content


def file_data_to_string(file_data: FileData) -> str:
    """Convert current or legacy persisted file content to a string.

    Args:
        file_data: File data whose content is a string or legacy list of strings.

    Returns:
        Content as a single string.

    Raises:
        TypeError: If content is neither a string nor a legacy list of strings.
    """
    return _normalize_content(file_data)


def create_file_data(
    content: str,
    created_at: str | None = None,
    encoding: str = "utf-8",
) -> FileData:
    """Create a `FileData` object with timestamps.

    Args:
        content: File content as string (plain text or base64-encoded binary).
        created_at: Optional creation timestamp (ISO format).
        encoding: Content encoding — `"utf-8"` for text, `"base64"` for binary.

    Returns:
        FileD`ata dict with content, encoding, and timestamps.
    """
    now = datetime.now(UTC).isoformat()

    return {
        "content": content,
        "encoding": encoding,
        "created_at": created_at or now,
        "modified_at": now,
    }


def _copy_file_data_with_content(file_data: FileData, content: str) -> FileData:
    """Clone `file_data` with replaced content, preserving timestamps when present.

    Unlike `update_file_data`, this carries `created_at`/`modified_at` through
    verbatim rather than restamping `modified_at`, since slicing a read window
    does not mutate the underlying file.

    Args:
        file_data: Source `FileData` whose encoding and timestamps are copied.
        content: Replacement content for the returned copy.

    Returns:
        A new `FileData` with `content` set and metadata carried over.
    """
    sliced_fd = FileData(
        content=content,
        encoding=file_data.get("encoding", "utf-8"),
    )
    if "created_at" in file_data:
        sliced_fd["created_at"] = file_data["created_at"]
    if "modified_at" in file_data:
        sliced_fd["modified_at"] = file_data["modified_at"]
    return sliced_fd


def normalize_read_bounds(offset: int, limit: int) -> tuple[int, int]:
    """Floor a requested read window at a zero offset and zero lines.

    Models occasionally emit degenerate `read_file` arguments (`offset=-1`,
    `limit=0`). Clamping `offset` keeps backends from reporting a line range
    that starts before line 1, which `ReadResult` rejects.

    Clamping `limit` is *not* sufficient on its own: flooring a negative limit
    at `0` produces a zero-length window, which still has no valid
    `start_line`/`end_line` pair. Callers must additionally treat a returned
    `limit` of `0` as an empty read — see `slice_read_response` below, or the
    equivalent short-circuits in the sandbox and LangSmith backends, which
    flag the result with `ReadResult.no_lines_requested`.

    The `int()` coercion is deliberate and load-bearing, not redundant with the
    annotations: `offset` and `limit` originate from model-supplied tool
    arguments, and the sandbox backend interpolates them into the source of a
    script it executes (`_READ_COMMAND_TEMPLATE`). Do not remove it.

    Args:
        offset: Requested 0-indexed line offset.
        limit: Requested maximum number of lines.

    Returns:
        Tuple of `(offset, limit)`, each coerced to `int` and floored at `0`.
    """
    normalized_offset, normalized_limit = max(int(offset), 0), max(int(limit), 0)
    if (normalized_offset, normalized_limit) != (offset, limit):
        logger.debug(
            "Clamped degenerate read window: offset %r -> %d, limit %r -> %d",
            offset,
            normalized_offset,
            limit,
            normalized_limit,
        )
    return normalized_offset, normalized_limit


def slice_read_response(
    file_data: FileData,
    offset: int,
    limit: int,
) -> ReadResult:
    """Slice file data to the requested line range without formatting.

    The returned `ReadResult` carries the raw (unformatted) window in
    `file_data`; line-number formatting is applied downstream by the
    middleware layer.

    Args:
        file_data: `FileData` dict.
        offset: Line offset (0-indexed).
        limit: Maximum number of lines.

    Both bounds are clamped through `normalize_read_bounds` before slicing, so
    a negative `offset` reads from the first line and a negative `limit` is
    treated as `0`.

    Returns:
        `ReadResult` with the sliced raw content and pagination metadata
            (`total_lines`, `start_line`, `end_line`, `next_offset`). The
            pagination fields are left unset for empty or whitespace-only
            content, and when the clamped `limit` is `0`; the zero-`limit`
            result additionally sets `no_lines_requested` so the middleware
            can tell the never-inspected window apart from a genuinely empty
            file. `error` is set instead when the offset exceeds the file
            length.
    """
    content = file_data_to_string(file_data)
    offset, limit = normalize_read_bounds(offset, limit)

    # Ordering note: blank content is reported before the zero-limit check, so a
    # whitespace-only file returns its content (which the middleware maps to the
    # empty-file reminder) rather than `""`, regardless of `limit`.
    if not content or content.strip() == "":
        return ReadResult(file_data=_copy_file_data_with_content(file_data, content))

    # Nothing was requested: flag the window as never inspected so the
    # middleware can tell it apart from a genuinely empty file, which arrives
    # via the blank-content branch above (its `ReadResult` is otherwise
    # identical: empty content, no pagination metadata).
    if limit == 0:
        return ReadResult(file_data=_copy_file_data_with_content(file_data, ""), no_lines_requested=True)

    # `splitlines(keepends=True)` retains each line's terminator, including
    # the absence of one on the final line. Joining with `""` therefore
    # round-trips the trailing-newline state of the file faithfully —
    # required so `edit()` can report EOF-newline mismatches accurately. It
    # also splits on CR / CRLF, so line indexing matches the LF-normalized
    # form without first rewriting the whole (potentially huge) string.
    lines = content.splitlines(keepends=True)
    start_idx = offset
    end_idx = min(start_idx + limit, len(lines))
    total_lines = len(lines)

    if start_idx >= total_lines:
        return ReadResult(error=f"Line offset {offset} exceeds file length ({total_lines} lines)")

    # Normalize line endings to LF, but only across the requested window.
    # State/Store backends may carry CRLF or CR content as written;
    # downstream tooling (edit match, grep, format) assumes LF.
    sliced = "".join(lines[start_idx:end_idx]).replace("\r\n", "\n").replace("\r", "\n")
    next_offset = end_idx if end_idx < total_lines else None
    return ReadResult(
        file_data=_copy_file_data_with_content(file_data, sliced),
        total_lines=total_lines,
        start_line=start_idx + 1,
        end_line=end_idx,
        next_offset=next_offset,
    )
