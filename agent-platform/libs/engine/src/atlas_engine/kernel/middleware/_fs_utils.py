"""文件工具的字符串 / 路径助手（行号排版、截断、路径校验、类型分类）。

前身是 kernel/backends/utils.py —— backends 包在协议上移 contracts、
实现类删净之后只剩这一个模块，「backends」这个名字已经名不副实，
解散进 middleware（消费者只有文件相关的三个中间件）。

FileData / ReadResult 相关的读窗口语义（create_file_data /
slice_read_response …）已随协议上移到 `atlas_engine.contracts.filesystem`
—— 这里只剩不碰文件数据结构的排版与路径助手。
"""

import logging
import os
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, Final, Literal, overload

logger = logging.getLogger(__name__)

EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
MAX_VIDEO_INPUT_BYTES: Final = 1024 * 1024 * 1024
"""Maximum raw video payload size accepted by `read_file` frame extraction."""

FileType = Literal["text", "image", "audio", "video", "file"]
"""Classification of a file by extension."""

_EXTENSION_TO_FILE_TYPE: dict[str, FileType] = {
    # Images (https://ai.google.dev/gemini-api/docs/image-understanding)
    ".png": "image",
    ".jpeg": "image",
    ".jpg": "image",
    ".webp": "image",
    ".gif": "image",
    ".heic": "image",
    ".heif": "image",
    # Video (https://ai.google.dev/gemini-api/docs/video-understanding)
    ".mp4": "video",
    ".mpeg": "video",
    ".mov": "video",
    ".avi": "video",
    ".flv": "video",
    ".mpg": "video",
    ".webm": "video",
    ".wmv": "video",
    ".3gpp": "video",
    # Audio (https://ai.google.dev/gemini-api/docs/audio)
    ".wav": "audio",
    ".mp3": "audio",
    ".aiff": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    # Files
    ".pdf": "file",
    ".ppt": "file",
    ".pptx": "file",
}
"""Extension-to-type mapping for non-text files.

Optional features may layer on additional classifications at the use site. For
example, `read_file` treats `.mkv` as video only when the optional video
dependencies are installed.

Derived from Google's multimodal API supported formats:

- Images: https://ai.google.dev/gemini-api/docs/image-understanding
- Video: https://ai.google.dev/gemini-api/docs/video-understanding
- Audio: https://ai.google.dev/gemini-api/docs/audio
"""

MAX_LINE_LENGTH = 5000
TOOL_RESULT_TOKEN_LIMIT = 20000  # Same threshold as eviction
TRUNCATION_GUIDANCE = "... [results truncated, try being more specific with your parameters]"



def sanitize_tool_call_id(tool_call_id: str) -> str:
    r"""Sanitize tool_call_id to prevent path traversal and separator issues.

    Replaces dangerous characters (., /, \) with underscores.
    """
    return tool_call_id.replace(".", "_").replace("/", "_").replace("\\", "_")


def format_content_with_line_numbers(
    content: str | list[str],
    start_line: int = 1,
) -> str:
    """Format file content with line numbers.

    Chunks lines longer than `MAX_LINE_LENGTH` with continuation markers
    (e.g., `5.1`, `5.2`). Line markers are separated from source content
    with two spaces so source tabs cannot be confused with a gutter separator.

    Args:
        content: File content as string or list of lines
        start_line: Starting line number

    Returns:
        Formatted content with line numbers and continuation markers
    """
    if isinstance(content, str):
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
    else:
        lines = content

    rows: list[tuple[str, str]] = []
    marker_width = 0
    for i, line in enumerate(lines):
        line_num = i + start_line
        # One slice per MAX_LINE_LENGTH chunk; short lines yield a single chunk.
        # `or [line]` keeps a row for a blank line, whose empty range would
        # otherwise drop it, so it still gets a gutter.
        chunks = [line[s : s + MAX_LINE_LENGTH] for s in range(0, len(line), MAX_LINE_LENGTH)] or [line]

        for chunk_idx, chunk in enumerate(chunks):
            marker = str(line_num) if chunk_idx == 0 else f"{line_num}.{chunk_idx}"
            rows.append((marker, chunk))
            marker_width = max(marker_width, len(marker))

    # The two-space marker/source separator is a load-bearing contract shared by
    # two downstream parsers that must stay in sync with the separator emitted
    # here:
    #   - `ReadFileContinuationNoticeMiddleware._is_numbered_read_file_row`
    #     (profiles/harness/_nvidia_nemotron_3_ultra.py) counts source rows to
    #     decide whether to append the continuation notice.
    #   - `ToolCallMessage._compact_line_gutter` (a coding TUI front-end, in a
    #     separate package: libs/code/.../tui/widgets/messages.py) re-justifies
    #     the gutter for display.
    # Both also tolerate the legacy `cat -n` tab. Shrinking this separator below
    # two spaces (or otherwise diverging) would silently break them; the
    # producer->consumer round-trip tests in both packages guard against that.
    return "\n".join(f"{marker:>{marker_width}}  {line}" for marker, line in rows)


def check_empty_content(content: str) -> str | None:
    """Check if content is empty and return warning message.

    Args:
        content: Content to check

    Returns:
        Warning message if empty, `None` otherwise
    """
    if not content or content.strip() == "":
        return EMPTY_CONTENT_WARNING
    return None


def _get_file_type(path: str) -> FileType:
    """Classify a file by its extension.

    Args:
        path: File path to classify.

    Returns:
        One of `"text"`, `"image"`, `"audio"`, `"video"`, or `"file"`.

            Defaults to `"text"` for unrecognized extensions.
    """
    return _EXTENSION_TO_FILE_TYPE.get(PurePosixPath(path).suffix.lower(), "text")


_VIDEO_EXTRA_EXTENSIONS: frozenset[str] = frozenset({".mkv"})
"""Video container extensions handled outside the Google-derived multimodal map.

These are intentionally absent from `_EXTENSION_TO_FILE_TYPE`, so a `read_file`
without the optional `[video]` extra returns them as a generic file block rather
than a native video block. Backends must still read them as binary — never
text-decode them — and `read_file` layers frame extraction on top only when the
`[video]` dependencies are installed.
"""


def _get_backend_read_file_type(path: str) -> FileType:
    """Classify a file for backend reads, forcing known video containers to binary.

    Backends decide binary-vs-text on `_get_file_type(...) != "text"`. Extensions
    in `_VIDEO_EXTRA_EXTENSIONS` are absent from `_EXTENSION_TO_FILE_TYPE`, so
    `_get_file_type` alone would treat them as text and corrupt the bytes (a raw
    UTF-8 decode of a video, or line-slicing a base64 blob). Classify them as
    `"video"` here so the binary read path runs on every backend.

    Args:
        path: File path to classify.

    Returns:
        `"video"` for `_VIDEO_EXTRA_EXTENSIONS`; otherwise the shared
            `_get_file_type` classification.
    """
    if PurePosixPath(path).suffix.lower() in _VIDEO_EXTRA_EXTENSIONS:
        return "video"
    return _get_file_type(path)


@overload
def truncate_if_too_long(result: list[str]) -> list[str]: ...


@overload
def truncate_if_too_long(result: str) -> str: ...


def truncate_if_too_long(result: list[str] | str) -> list[str] | str:
    """Truncate list or string result if it exceeds token limit (rough estimate: 4 chars/token)."""
    if isinstance(result, list):
        total_chars = sum(len(item) for item in result)
        if total_chars > TOOL_RESULT_TOKEN_LIMIT * 4:
            return result[: len(result) * TOOL_RESULT_TOKEN_LIMIT * 4 // total_chars] + [TRUNCATION_GUIDANCE]  # noqa: RUF005  # Concatenation preferred for clarity
        return result
    # string
    if len(result) > TOOL_RESULT_TOKEN_LIMIT * 4:
        return result[: TOOL_RESULT_TOKEN_LIMIT * 4] + "\n" + TRUNCATION_GUIDANCE
    return result


# Characters that mark a glob path component as a wildcard segment for the
# purposes of `_glob_anchor`. Keep in sync with the wcmatch flags used by the
# filesystem middleware (`BRACE | GLOBSTAR`).
_GLOB_WILDCARD_CHARS = frozenset("*?[{")


def _glob_anchor(pattern: str) -> str:
    """Return the longest leading directory of `pattern` with no wildcards.

    For `/secrets/**` returns `/secrets`; for `/a/*/b` returns `/a`; for a
    pattern with a wildcard at or near the root (`/**/secrets`, `/*/foo`)
    falls back to `/`. The root fallback causes overlap checks to match
    *any* subtree — conservative over-gating, since we cannot statically
    pin down where the rule could resolve. Callers wanting precise gating
    should anchor the rule's leading components.
    """
    parts = PurePosixPath(to_posix_path(pattern)).parts
    safe: list[str] = []
    for part in parts:
        if any(c in _GLOB_WILDCARD_CHARS for c in part):
            break
        safe.append(part)
    if not safe:
        return "/"
    return str(PurePosixPath(*safe))


def _paths_overlap(call_path: str, rule_anchor: str) -> bool:
    """Return True if the subtree at `call_path` intersects the subtree at `rule_anchor`.

    Two subtrees overlap when one is a (component-wise) prefix of the other,
    or they're equal. Comparison runs on `PurePosixPath` components, so
    `/secret` does not overlap `/secrets`. The root `/` overlaps everything.
    """
    a = PurePosixPath(call_path)
    b = PurePosixPath(rule_anchor)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def to_posix_path(path: str) -> str:
    r"""Normalize backslash separators to forward slashes for `PurePosixPath` use.

    Backends running on Windows return OS-native paths using backslashes.
    `PurePosixPath` treats backslashes as literal filename characters,
    so `PurePosixPath(r"C:\a\b").name` yields the full string instead
    of `"b"`. Normalize before constructing a `PurePosixPath`.

    This is best-effort: a POSIX directory literally named with a backslash
    will also be rewritten. That trade-off is accepted because such filenames
    are vanishingly rare in practice and the alternative (gating on `os.sep`)
    fails when a Windows-style path is handed to a non-Windows process.

    Args:
        path: Path string that may use backslash separators.

    Returns:
        The same path with every `\\` replaced by `/`.

            Inputs that already use forward slashes are returned unchanged.
    """
    return path.replace("\\", "/")


def validate_path(path: str, *, allowed_prefixes: Sequence[str] | None = None) -> str:
    r"""Validate and normalize file path for security.

    Ensures paths are safe to use by preventing directory traversal attacks
    and enforcing consistent formatting. All paths are normalized to use
    forward slashes and start with a leading slash.

    This function is designed for virtual filesystem paths and rejects
    Windows absolute paths (e.g., `C:/...`, `F:/...`) to maintain consistency
    and prevent path format ambiguity.

    Args:
        path: The path to validate and normalize.
        allowed_prefixes: Optional list of allowed path prefixes.

            If provided, the normalized path must start with one of
            these prefixes.

    Returns:
        Normalized canonical path starting with `/` and using forward slashes.

    Raises:
        ValueError: If path contains traversal sequences (`..` or `~`), is a
            Windows absolute path (e.g., `C:/...`), or does not start with an
            allowed prefix when `allowed_prefixes` is specified.

    Example:
        ```python
        validate_path("foo/bar")  # Returns: "/foo/bar"
        validate_path("/./foo//bar")  # Returns: "/foo/bar"
        validate_path("../etc/passwd")  # Raises ValueError
        validate_path(r"C:\\Users\\file.txt")  # Raises ValueError
        validate_path("/data/file.txt", allowed_prefixes=["/data/"])  # OK
        validate_path("/etc/file.txt", allowed_prefixes=["/data/"])  # Raises ValueError
        ```
    """
    # Check for traversal as a path component (not substring) to avoid
    # false-positive rejection of legitimate filenames like "foo..bar.txt"
    parts = PurePosixPath(to_posix_path(path)).parts
    if ".." in parts or path.startswith("~"):
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)

    # Reject Windows absolute paths (e.g., C:\..., D:/...)
    if re.match(r"^[a-zA-Z]:", path):
        msg = f"Windows absolute paths are not supported: {path}. Please use virtual paths starting with / (e.g., /workspace/file.txt)"
        raise ValueError(msg)

    normalized = os.path.normpath(path)
    normalized = normalized.replace("\\", "/")

    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    # Defense-in-depth: verify normpath didn't produce traversal
    if ".." in normalized.split("/"):
        msg = f"Path traversal detected after normalization: {path} -> {normalized}"
        raise ValueError(msg)

    if allowed_prefixes is not None and not any(normalized.startswith(prefix) for prefix in allowed_prefixes):
        msg = f"Path must start with one of {allowed_prefixes}: {path}"
        raise ValueError(msg)

    return normalized


def _normalize_path(path: str | None) -> str:
    """Normalize a path to canonical form.

    Converts path to absolute form starting with /, removes trailing slashes
    (except for root), and validates that the path is not empty.

    Args:
        path: Path to normalize (None defaults to "/")

    Returns:
        Normalized path starting with / (without trailing slash unless it's root)

    Raises:
        ValueError: If path is invalid (empty string after strip)

    Example:
        _normalize_path(None) -> "/"
        _normalize_path("/dir/") -> "/dir"
        _normalize_path("dir") -> "/dir"
        _normalize_path("/") -> "/"
    """
    path = path or "/"
    if not path or path.strip() == "":
        msg = "Path cannot be empty"
        raise ValueError(msg)

    normalized = path if path.startswith("/") else "/" + path

    # Only root should have trailing slash
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")

    return normalized


def _filter_files_by_path(files: dict[str, Any], normalized_path: str) -> dict[str, Any]:
    """Filter files dict by normalized path, handling exact file matches and directory prefixes.

    Expects a normalized path from `_normalize_path` (no trailing slash except root).

    Args:
        files: Dictionary mapping file paths to file data
        normalized_path: Normalized path from `_normalize_path` (e.g., "/", "/dir", "/dir/file")

    Returns:
        Filtered dictionary of files matching the path

    Example:
        files = {"/dir/file": {...}, "/dir/other": {...}}
        _filter_files_by_path(files, "/dir/file")  # Returns {"/dir/file": {...}}
        _filter_files_by_path(files, "/dir")       # Returns both files
    """
    # Check if path matches an exact file
    if normalized_path in files:
        return {normalized_path: files[normalized_path]}

    # Otherwise treat as directory prefix
    if normalized_path == "/":
        # Root directory - match all files starting with /
        return {fp: fd for fp, fd in files.items() if fp.startswith("/")}
    # Non-root directory - add trailing slash for prefix matching
    dir_prefix = normalized_path + "/"
    return {fp: fd for fp, fd in files.items() if fp.startswith(dir_prefix)}








# -------- Structured helpers for composition --------










def _group_adjacent_lines(displayed_lines: dict[int, str]) -> list[list[tuple[int, str]]]:
    """Split `{line_number: text}` into runs of consecutive line numbers."""
    groups: list[list[tuple[int, str]]] = []
    for item in sorted(displayed_lines.items()):
        if not groups or item[0] > groups[-1][-1][0] + 1:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


_REGEX_SIGNAL_RE = re.compile(
    r"\|"  # alternation
    r"|\.\*"  # `.*` wildcard
    r"|\.\+"  # `.+` wildcard
    r"|\\[.wWdDsSbB(){}\[\]|+*?^$]"  # escaped regex metacharacters / classes
)
"""Strong signals that a pattern was written as a regex rather than literal text.

Deliberately conservative: bare `.`, `(`, `)`, `[`, `]`, `?`, `^`, `$` are
omitted because they appear routinely in literal code searches (e.g.
`self.tools`, `def __init__(self):`, `arr[0]`), which would cause false hints.
"""




