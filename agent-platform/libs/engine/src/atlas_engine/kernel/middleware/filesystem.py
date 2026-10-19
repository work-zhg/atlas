"""Middleware for providing filesystem tools to an agent."""
# ruff: noqa: E501

import concurrent.futures
import mimetypes
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import wcmatch.glob as wcglob
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.messages.content import ContentBlock
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from pydantic import BaseModel, Field

from atlas_engine.contracts import (
    DeleteResult,
    EditResult,
    FilesystemProtocol,
    ReadResult,
    WriteResult,
)
from atlas_engine.kernel.middleware._fs_utils import (
    _EXTENSION_TO_FILE_TYPE,
    _GLOB_WILDCARD_CHARS,
    FileType,
    _get_file_type,
    _glob_anchor,
    _paths_overlap,
    check_empty_content,
    format_content_with_line_numbers,
    truncate_if_too_long,
    validate_path,
)
from atlas_engine.kernel.middleware._message_eviction import (
    _aoffload_tool_message_content,
    _create_content_preview,
    _extract_text_from_message,
    _offload_tool_message_content,
)
from atlas_engine.kernel.middleware._utils import append_to_system_message

# `ChatOpenAI`, `AzureChatOpenAI`, and `ChatGoogleGenerativeAI` accept non-PDF
# `file` blocks such as `.docx` and `.pptx`. `ModelProfile` only encodes PDF
# support today, so these providers get a hard-coded pass until profiles can
# describe support for other office and document formats.
try:
    from langchain_openai import AzureChatOpenAI as _AzureChatOpenAI
    from langchain_openai import ChatOpenAI as _ChatOpenAI
except ImportError:
    _OPENAI_FILE_MODEL_TYPES: tuple[type[Any], ...] = ()
else:
    _OPENAI_FILE_MODEL_TYPES = (_AzureChatOpenAI, _ChatOpenAI)

try:
    from langchain_google_genai import ChatGoogleGenerativeAI as _ChatGoogleGenerativeAI
except ImportError:
    _GOOGLE_FILE_MODEL_TYPES: tuple[type[Any], ...] = ()
else:
    _GOOGLE_FILE_MODEL_TYPES = (_ChatGoogleGenerativeAI,)

if TYPE_CHECKING:
    from langchain.chat_models import BaseChatModel

_FS_WCMATCH_FLAGS = wcglob.BRACE | wcglob.GLOBSTAR
"""wcmatch flags enabling brace expansion and `**` globstar recursion."""

_SYNC_GLOB_WORKERS = 4
"""Thread-pool size for synchronous glob operations."""

FilesystemOperation = Literal["read", "write"]
"""Classification of filesystem tools as read-only or mutating."""


_READ_FILE_MEDIA_RESULT: Final = "read_file_media_result"
"""`additional_kwargs` key marking synthetic `HumanMessage` media from `read_file`."""


_MULTIMODAL_BLOCK_TYPES: Final = frozenset(_EXTENSION_TO_FILE_TYPE.values())
"""Content block types `read_file` may emit that require multimodal model support.

Derived from `_EXTENSION_TO_FILE_TYPE`'s values (`"text"` never appears there,
since it's `_get_file_type`'s default for unmapped extensions).
"""

_PDF_MIME_TYPE: Final = "application/pdf"


def _emit_file_written(path: str, size_bytes: int) -> None:
    """经 custom stream 通道补发 file.written。

    ★ 文件系统外置（OSS）之后，写入不再经过 graph state，于是 runner 的
      `files_delta` 推导不出任何东西 —— Inspector 的文件页会直接失明。
      这里由中间件补发，对任何 FilesystemProtocol 实现都成立。

    图外调用（测试 / 脚本）没有 writer，补发是尽力而为。
    """
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"kind": "file.written", "path": path, "size_bytes": size_bytes})
    except Exception:  # noqa: BLE001 - 无 writer 时静默跳过是有意的
        return


def _is_read_file_media_result(message: AnyMessage) -> bool:
    """Return whether `message` carries media emitted by a `read_file` tool result."""
    return isinstance(message, HumanMessage) and message.additional_kwargs.get(_READ_FILE_MEDIA_RESULT) is True


def _move_media_results_after_tool_results(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Keep synthetic media messages after the tool-result batch they describe.

    Tool-call providers require every `ToolMessage` for an assistant tool-call
    batch to arrive before any non-tool message. Video reads attach sampled
    frames as a synthetic `HumanMessage`; when multiple tools run in the same
    turn this helper keeps those attachments behind the full batch.
    """
    reordered: list[AnyMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        reordered.append(message)
        i += 1
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue

        batch: list[AnyMessage] = []
        while i < len(messages):
            next_message = messages[i]
            if isinstance(next_message, ToolMessage) or _is_read_file_media_result(next_message):
                batch.append(next_message)
                i += 1
                continue
            break
        if batch:
            reordered.extend(message for message in batch if isinstance(message, ToolMessage))
            reordered.extend(message for message in batch if _is_read_file_media_result(message))
    return reordered


_PROFILE_FIELD_BY_BLOCK_TYPE: Final = {"image": "image_inputs", "audio": "audio_inputs", "video": "video_inputs", "file": "pdf_inputs"}
"""`ModelProfile` field gating each block type. `file` only applies to PDF `mime_type`; other
file types have no field yet and are handled separately via provider class checks."""

_TOOL_MESSAGE_FIELD_BY_BLOCK_TYPE: Final = {"image": "image_tool_message", "file": "pdf_tool_message"}
"""Extra `ModelProfile` field that can gate a block type specifically within a `ToolMessage`."""


def _model_tolerates_non_pdf_files(model: "BaseChatModel | None") -> bool:
    """Whether `model` is a provider class known to accept non-PDF `file` blocks."""
    return isinstance(model, _OPENAI_FILE_MODEL_TYPES + _GOOGLE_FILE_MODEL_TYPES)


def _multimodal_block_supported(
    block: ContentBlock,
    *,
    profile: Mapping[str, Any],
    tolerates_non_pdf_files: bool,
    in_tool_message: bool,
) -> bool:
    """Check whether `profile` (plus the hard-coded provider exception) accepts `block`.

    Missing `ModelProfile` fields default to supported, since profile coverage is
    incomplete. Only an explicit `False` rejects a block type.
    """
    block_type = block["type"]
    if block_type == "file" and "base64" not in block:
        # URL-/file-ID-backed file references are provider-managed and often don't
        # include a `mime_type`, so leave them untouched.
        return True
    if block_type == "file" and block.get("mime_type") != _PDF_MIME_TYPE:
        # Non-PDF base64 `file` blocks (`.docx`, `.pptx`, ...) aren't described
        # by any `ModelProfile` field yet; only the hard-coded tolerant
        # providers pass.
        return tolerates_non_pdf_files

    field = _PROFILE_FIELD_BY_BLOCK_TYPE.get(block_type)
    if field is None:
        return True
    if in_tool_message:
        tool_field = _TOOL_MESSAGE_FIELD_BY_BLOCK_TYPE.get(block_type)
        if tool_field and profile.get(tool_field) is False:
            return False
    return profile.get(field) is not False


def _unsupported_multimodal_placeholder(block: ContentBlock, message: AnyMessage) -> ContentBlock:
    """Build the text block replacing a multimodal block the model can't accept."""
    mime_type = block.get("mime_type", "unknown")
    path = message.additional_kwargs.get("read_file_path", "the requested file")
    return cast(
        "ContentBlock",
        {
            "type": "text",
            "text": f"[read_file: {path} was not attached because this model does not support {block['type']} content ({mime_type}).]",
        },
    )


def _scrub_message_multimodal_content(message: AnyMessage, *, profile: Mapping[str, Any], tolerates_non_pdf_files: bool) -> AnyMessage:
    """Return `message` unchanged, or a copy with unsupported blocks replaced by placeholders."""
    if not isinstance(message, (ToolMessage, HumanMessage)):
        return message

    in_tool_message = isinstance(message, ToolMessage)
    blocks = message.content_blocks
    new_blocks = [
        block
        if block["type"] not in _MULTIMODAL_BLOCK_TYPES
        or _multimodal_block_supported(block, profile=profile, tolerates_non_pdf_files=tolerates_non_pdf_files, in_tool_message=in_tool_message)
        else _unsupported_multimodal_placeholder(block, message)
        for block in blocks
    ]
    if new_blocks == blocks:
        return message
    return message.model_copy(update={"content": new_blocks})


def _scrub_unsupported_multimodal_content(messages: list[AnyMessage], model: "BaseChatModel | None") -> list[AnyMessage]:
    """Replace multimodal content blocks `model.profile` marks unsupported.

    Some providers return a non-retryable 400 when sent a content block they
    don't support (e.g. a `file` block whose `mime_type` isn't
    `application/pdf`, produced when `read_file` reads a `.docx`), which would
    otherwise end the thread. Swapping the unsupported block for a text
    placeholder here before the request reaches the model.

    A `model` with no `profile` (including `None` `model`, e.g. in tests) is
    treated as an empty profile rather than skipped: `ModelProfile` is often
    absent for models `langchain_anthropic` doesn't have a static entry for
    (e.g. `ChatAnthropic(model="claude-3-5-sonnet-latest")`), and the
    provider-based non-PDF `file` gate doesn't depend on profile data at all —
    skipping the whole scrub in that case would silently leave the exact
    `.docx`-on-Anthropic bug this fixes unfixed for those models. An empty
    profile still defaults every per-field check to "supported."
    """
    profile = model.profile if model is not None else None
    if not isinstance(profile, dict):
        profile = {}
    tolerates_non_pdf_files = _model_tolerates_non_pdf_files(model)
    return [_scrub_message_multimodal_content(message, profile=profile, tolerates_non_pdf_files=tolerates_non_pdf_files) for message in messages]


def _get_read_file_type(path: str) -> FileType:
    """Classify a file for `read_file`, gating optional video extensions."""
    file_type = _get_file_type(path)
    return file_type


@dataclass
class FilesystemPermission:
    """A single access rule for filesystem operations."""

    operations: list[FilesystemOperation]
    paths: list[str]
    mode: Literal["allow", "deny"] = "allow"
    """Effect when a tool call matches this rule:

    - `"allow"` (default): the call proceeds.
    - `"deny"`: the tool returns a permission-denied error.

    ★ 没有 `"interrupt"`（原实现经 HumanInTheLoopMiddleware 暂停等人确认）：它靠
      LangGraph 的 interrupt() + checkpointer，而 Atlas 不用 checkpointer，审批走
      ApprovalMiddleware 的挂起（contracts/suspension.py）。要按路径审批，在那里加。
    """

    def __post_init__(self) -> None:
        """Validate the mode and permission path patterns."""
        if self.mode not in ("allow", "deny"):
            # 调用方只认 "deny"：别的值会被当成放行 —— 报出来，不静默
            msg = f"Permission mode must be 'allow' or 'deny': {self.mode!r}"
            raise ValueError(msg)
        for path in self.paths:
            if not path.startswith("/"):
                msg = f"Permission path must start with '/': {path!r}"
                raise ValueError(msg)
            parts = PurePosixPath(path.replace("\\", "/")).parts
            if ".." in parts:
                msg = f"Permission path must not contain '..': {path!r}"
                raise ValueError(msg)
            if "~" in parts:
                msg = f"Permission path must not contain '~': {path!r}"
                raise NotImplementedError(msg)


def _check_fs_permission(
    rules: list[FilesystemPermission],
    operation: FilesystemOperation,
    path: str,
) -> Literal["allow", "deny"]:
    for rule in rules:
        if operation not in rule.operations:
            continue
        if any(wcglob.globmatch(path, pattern, flags=_FS_WCMATCH_FLAGS) for pattern in rule.paths):
            return rule.mode
    return "allow"


def _wildcard_delete_overlap(pattern: str, anchor: str, target: str) -> bool:
    """Check whether a wildcard deny pattern overlaps a recursive delete target.

    Args:
        pattern: The original glob pattern (e.g. ``/work/*.log``).
        anchor: The longest wildcard-free prefix of ``pattern``.
        target: The absolute path being recursively deleted.

    Returns:
        True if the pattern's matches intersect the delete subtree.
    """
    # Root anchor ("/**/x"): pattern can match anywhere, block all.
    if anchor == "/":
        return True
    # Target directly matches the glob: block.
    if wcglob.globmatch(target, pattern, flags=_FS_WCMATCH_FLAGS):
        return True
    # Anchor is inside the delete subtree: recursive delete would remove
    # matching descendants — block.
    if PurePosixPath(anchor).is_relative_to(PurePosixPath(target)):
        return True
    # Target is below the anchor: safe to allow ONLY when the pattern suffix
    # is a single, non-** component (fixed depth) AND no ancestor of the
    # target matches the glob. "/work/*.log" can never match anything under
    # "/work/notes.txt". But "/work/*" matches "/work/app", so deleting
    # "/work/app/child" mutates a denied path's contents and must be blocked.
    # Patterns with directory wildcards ("/work/*/secrets") could match
    # descendants of the target, so fail closed for those.
    if not PurePosixPath(target).is_relative_to(PurePosixPath(anchor)):
        return False
    anchor_parts = PurePosixPath(anchor).parts
    pattern_parts = PurePosixPath(pattern).parts
    suffix = pattern_parts[len(anchor_parts) :]
    if len(suffix) != 1 or "**" in suffix[0]:
        return True
    # Check whether any ancestor of the target (between anchor and target)
    # matches the glob. If so, the target is inside a denied directory's
    # subtree.
    target_parts = PurePosixPath(target).parts
    return any(
        wcglob.globmatch(
            str(PurePosixPath(*target_parts[:depth])),
            pattern,
            flags=_FS_WCMATCH_FLAGS,
        )
        for depth in range(len(anchor_parts), len(target_parts))
    )


def _delete_target_may_have_descendants(
    filesystem: Any, target: str, *, permissions_configured: bool
) -> bool:
    """`delete` 是否要走保守的递归权限检查。

    ★ 原实现用 `filesystem.ls(target)` 判断是不是目录。`ls` 已随三个检索工具
      一起并入 `search` —— 对象存储上没有目录，只有前缀。现在改成：
      该前缀下还有别的 key ⇒ 有子节点。

    没有权限规则时直接返回 False：无子树可保护，保守检查纯属浪费一次列举。
    探测失败一律返回 True（保守）—— 判不准时宁可多做一次权限检查，
    也不能漏掉一棵被规则保护的子树。
    """
    if not permissions_configured:
        return False
    try:
        result = filesystem.search(target.lstrip("/"), max_keys=2)
    except (NotImplementedError, AttributeError):
        return True
    if result.error is not None:
        return True
    others = [k for k in result.keys if k.rstrip("/") != target.lstrip("/").rstrip("/")]
    return bool(others or result.common_prefixes)


async def _adelete_target_may_have_descendants(
    filesystem: Any, target: str, *, permissions_configured: bool
) -> bool:
    """`_delete_target_may_have_descendants` 的异步版。"""
    if not permissions_configured:
        return False
    try:
        result = await filesystem.asearch(target.lstrip("/"), max_keys=2)
    except (NotImplementedError, AttributeError):
        return True
    if result.error is not None:
        return True
    others = [k for k in result.keys if k.rstrip("/") != target.lstrip("/").rstrip("/")]
    return bool(others or result.common_prefixes)


def _find_delete_deny_patterns_for_leaf(rules: list[FilesystemPermission], target: str) -> list[str]:
    """Resolve delete permission for a confirmed plain file: first matching rule wins.

    Mirrors `_check_fs_permission`'s ordering, but returns the matched
    pattern(s) so the delete tool's error message can cite them.
    """
    for rule in rules:
        if "write" not in rule.operations:
            continue
        matched = [pattern for pattern in rule.paths if wcglob.globmatch(target, pattern, flags=_FS_WCMATCH_FLAGS)]
        if not matched:
            continue
        return matched if rule.mode == "deny" else []
    return []


def _find_delete_deny_patterns(
    rules: list[FilesystemPermission],
    target: str,
    *,
    has_descendants: bool = True,
) -> list[str]:
    """Return deny-write patterns that block deleting `target`.

    A recursive delete removes `target` and all descendants, so when
    `has_descendants` is `True` a deny-write pattern blocks the operation
    when it could match `target` or anything in its subtree, regardless of
    rule order -- an earlier allow rule can't guarantee every descendant is
    safe, since a later, more specific deny could still apply to one of them.
    Sibling file globs that cannot match anything inside the deleted subtree
    (e.g. deny `/work/*.log` when deleting `/work/notes.txt`) do not block.

    When `has_descendants` is `False` (a confirmed plain file, see
    `_delete_target_may_have_descendants`), there's no subtree to protect, so
    `target` is resolved the same way `write_file`/`edit_file` resolve
    permissions: the first rule (in declaration order) that matches wins.

    Literal (wildcard-free) deny patterns use a subtree-overlap check: a deny
    on a directory blocks deleting anything inside it and blocks deleting an
    ancestor that contains it. Wildcard patterns are handled by
    `_wildcard_delete_overlap`, which also blocks when the glob matches an
    ancestor of `target` (deleting `/work/app/child` under a deny on `/work/*`
    mutates the denied `/work/app`), while still allowing siblings that can
    never contain a match (deny `/work/*.log` vs `/work/notes.txt`).

    Args:
        rules: Filesystem permission rules.
        target: Absolute, validated path being deleted.
        has_descendants: Whether `target` may have entries nested under it.
            Pass `False` only once the filesystem has confirmed it's a leaf.

    Returns:
        Matching deny-write patterns, or an empty list if the delete is allowed.
    """
    if not has_descendants:
        return _find_delete_deny_patterns_for_leaf(rules, target)

    denying: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.mode != "deny" or "write" not in rule.operations:
            continue
        for pattern in rule.paths:
            if pattern in seen:
                continue
            anchor = _glob_anchor(pattern)
            if any(c in _GLOB_WILDCARD_CHARS for c in pattern):
                overlaps = _wildcard_delete_overlap(pattern, anchor, target)
            else:
                # Literal pattern (no wildcards): keep the original subtree-overlap
                # check so that a deny on "/work" blocks deletes of "/work/sub".
                overlaps = _paths_overlap(target, anchor)
            if overlaps:
                seen.add(pattern)
                denying.append(pattern)
    return denying


def _format_file_paths(paths: list[str]) -> str:
    """Format filesystem path lists for tool output."""
    if not paths:
        return "No files found"
    return str(truncate_if_too_long(paths))


def _remaining_lines_notice(read_result: ReadResult) -> str:
    """Render the read pagination notice when the filesystem returned a partial window.

    Args:
        read_result: Filesystem read result carrying the pagination metadata
            (`start_line`, `end_line`, `next_offset`, `total_lines`).

    Returns:
        A model-facing notice describing the window that was read and where to
            resume, or an empty string when no window metadata is present or the
            window already reached the end of the file (nothing more to read).
    """
    start_line = read_result.start_line
    end_line = read_result.end_line
    next_offset = read_result.next_offset
    if start_line is None or end_line is None or next_offset is None:
        return ""

    total_lines = read_result.total_lines
    read_count = end_line - start_line + 1
    read_unit = "line" if read_count == 1 else "lines"
    if total_lines is None:
        return f"\n\n[Read {read_count} {read_unit} (lines {start_line}-{end_line}). More lines remain from offset {next_offset}.]"
    if end_line >= total_lines:
        return ""

    remaining = total_lines - end_line
    remaining_unit = "line" if remaining == 1 else "lines"
    return (
        f"\n\n[Read {read_count} {read_unit} "
        f"(lines {start_line}-{end_line} of {total_lines} total). "
        f"{remaining} {remaining_unit} remaining from offset {next_offset}.]"
    )


def _clamped_offset_notice(offset: int) -> str:
    """Disclose that a negative requested offset was read from the file start.

    Filesystems clamp a negative `offset` to `0` rather than erroring, so without
    this the model sees a correct-looking gutter starting at line 1 and no
    indication its request was reinterpreted. `_remaining_lines_notice` cannot
    carry the disclosure: it returns an empty string once the window reaches the
    end of the file, which is exactly the common degenerate case
    (`offset=-1` with a default `limit`).

    Args:
        offset: Offset as requested by the caller, before clamping.

    Returns:
        A model-facing notice when `offset` was negative, else an empty string.
    """
    if offset >= 0:
        return ""
    return f"\n\n[Requested offset {offset} is before the start of the file; read from line 1 instead.]"


EMPTY_CONTENT_WARNING = "System reminder: File exists but has empty contents"
NO_LINES_REQUESTED_WARNING = (
    "System reminder: no lines were read because `limit` was {limit}. The file was "
    "not inspected and may have contents; retry with `limit` >= 1 to read it."
)
"""Reported when a read requested zero lines.

Distinct from `EMPTY_CONTENT_WARNING` on purpose: the `read_file` description
teaches the model that the empty-contents reminder means the file itself is
empty, so reusing it for a zero-line window would state something false about
the filesystem that a following `write_file` could act on destructively.

Filesystems declare the zero-line window with `ReadResult.no_lines_requested`,
so an inspected-but-empty file (which otherwise arrives identically: empty
content, no pagination metadata) keeps the empty-file reminder instead.
"""


DEFAULT_READ_OFFSET = 0
DEFAULT_READ_LIMIT = 100
# Template for truncation message in read_file
# {file_path} will be filled in at runtime
READ_FILE_TRUNCATION_MSG = (
    "\n\n[Output was truncated due to size limits. "
    "The file content is very large. "
    "Consider reformatting the file to make it easier to navigate. "
    "For example, if this is JSON, use execute(command='jq . {file_path}') to pretty-print it with line breaks. "
    "For other formats, you can use appropriate formatting tools to split long lines.]"
)

# Approximate number of characters per token for truncation calculations.
# Using 4 chars per token as a conservative approximation (actual ratio varies by content)
# This errs on the high side to avoid premature eviction of content that might fit
NUM_CHARS_PER_TOKEN = 4


def _truncate_paginated_read(
    content: str,
    file_path: str,
    read_result: ReadResult,
    token_limit: int | None,
) -> str:
    """Truncate a paginated read without skipping undisplayed source lines.

    The filesystem computes the pagination notice from the full window it
    returned, but the char budget may drop trailing rows from what the model
    actually sees. Appending the filesystem's notice verbatim would then advertise
    a `next_offset` past those dropped lines, so a re-read would silently skip
    them. This recomputes the notice from the last *complete* rendered row that
    still fits, and falls back to the size warning alone (no stale offset) when
    not even one full source line fits.

    Args:
        content: Line-numbered content produced by
            `format_content_with_line_numbers` (a marker followed by two spaces
            and the source content).
        file_path: Path used to format the truncation message.
        read_result: Filesystem read result carrying the window metadata; the
            adjusted `next_offset` is derived from its 1-indexed line range.
        token_limit: Char budget is `NUM_CHARS_PER_TOKEN * token_limit`; when
            falsy, content is returned with its notice untouched.

    Returns:
        The (possibly truncated) content with a notice that never overstates
            which source lines were shown.

    Examples:
        If the filesystem returns source lines 11-20 with `next_offset=20`, but
        the budget fits only through line 14, the returned notice reports lines
        11-14 and tells the caller to resume from offset 14 rather than 20.

        A long source line may be rendered as rows `14` and `14.1`. If the
        budget fits row `14` but not `14.1`, neither row is retained: the notice
        reports line 13 as the last displayed line and resumes from offset 13.
    """
    notice = _remaining_lines_notice(read_result)
    if not token_limit or len(content) + len(notice) < NUM_CHARS_PER_TOKEN * token_limit:
        return content + notice

    truncation_msg = READ_FILE_TRUNCATION_MSG.format(file_path=file_path)
    threshold = NUM_CHARS_PER_TOKEN * token_limit
    if read_result.start_line is not None and read_result.end_line is not None:
        # Build the safe places where the content can be truncated. A long source
        # line may span rendered rows numbered `12`, `12.1`, and so on, so cutting
        # at every newline could keep only part of that source line. `position`
        # tracks each rendered row's end in `content`; comparing the integer part
        # of adjacent row markers records a boundary only after the final row for
        # a source line. The loop below uses these boundaries to find the latest
        # complete source line that fits alongside the truncation message and the
        # pagination notice.
        rows = content.split("\n")
        position = 0
        boundaries: list[tuple[int, int]] = []
        for index, row in enumerate(rows):
            position += len(row)
            marker = row.lstrip().partition("  ")[0].partition(".")[0]
            source_line = int(marker)
            # Rows numbered past the window's last source line are not file
            # content: a byte-capped filesystem page appends its own truncation
            # banner (preceded by a blank line), which `format_content_with_line_numbers`
            # then numbers as `end_line + 1`, `end_line + 2`, .... Stop before
            # them so a banner row is never chosen as a boundary — resuming from
            # its inflated number would overshoot `total_lines` and skip real
            # lines. Rows are numbered monotonically, so the first out-of-range
            # row means the rest are banner too.
            if source_line > read_result.end_line:
                break
            next_source_line = None
            if index + 1 < len(rows):
                next_marker = rows[index + 1].lstrip().partition("  ")[0].partition(".")[0]
                next_source_line = int(next_marker)
            if next_source_line != source_line:
                boundaries.append((position, source_line))
            position += 1

        # Only advertise source lines whose complete rendered rows fit. If the
        # byte cut landed partway through a row, resuming after that row would
        # silently skip its undisplayed tail. `next_offset` is the 0-indexed line
        # after the last one shown, which for a 1-indexed `end_line` is exactly
        # `end_line` (no reliance on how the request `offset` maps to `start_line`).
        for boundary, end_line in reversed(boundaries):
            adjusted_result = ReadResult(
                total_lines=read_result.total_lines,
                start_line=read_result.start_line,
                end_line=end_line,
                next_offset=end_line,
            )
            adjusted_notice = _remaining_lines_notice(adjusted_result)
            if boundary + len(truncation_msg) + len(adjusted_notice) <= threshold:
                return content[:boundary] + truncation_msg + adjusted_notice

    # No complete source line fits. Keep the size warning but omit the
    # filesystem's stale pagination offset.
    max_content_length = max(0, threshold - len(truncation_msg))
    return content[:max_content_length] + truncation_msg


class ReadFileSchema(BaseModel):
    """Input schema for the `read_file` tool."""

    file_path: str = Field(description="Absolute path to the file to read. Must be absolute, not relative.")

    offset: int = Field(
        default=DEFAULT_READ_OFFSET,
        description="Line number to start reading from (0-indexed). Use for pagination of large files.",
    )

    limit: int = Field(
        default=DEFAULT_READ_LIMIT,
        description="Maximum number of lines to read. Use for pagination of large files.",
    )


class WriteFileSchema(BaseModel):
    """Input schema for the `write_file` tool."""

    file_path: str = Field(description="Absolute path where the file should be written. Must be absolute, not relative.")

    content: str = Field(description="The text content to write to the file. This parameter is required.")


class EditFileSchema(BaseModel):
    """Input schema for the `edit_file` tool."""

    file_path: str = Field(description="Absolute path to the file to edit. Must be absolute, not relative.")

    old_string: str = Field(description="The exact text to find and replace. Must be unique in the file unless replace_all is True.")

    new_string: str = Field(description="The text to replace old_string with. Must be different from old_string.")

    replace_all: bool = Field(
        default=False,
        description="If True, replace all occurrences of old_string. If False (default), old_string must be unique.",
    )


class DeleteSchema(BaseModel):
    """Input schema for the `delete` tool."""

    file_path: str = Field(description="Absolute path to the file to delete. Must be absolute, not relative.")


SEARCH_TOOL_DESCRIPTION = """Lists files in the session workspace by path prefix and file name.

Usage:
- `prefix` narrows to a subtree (`"reports/"`); omit it to list the workspace root.
- `pattern` filters by file name (`"*.md"`). Use `**/` to recurse
  (`"**/*.csv"` finds every csv at any depth).
- Without `**`, only one level is listed and sub-directory names come back
  separately — object storage has no real directories, they are derived from keys.
- **This tool does not search file contents.** To find text inside files, use the
  execute tool with `grep`/`rg`.
- Results are capped; a note is appended when the cap is hit, narrow the prefix.
"""


class SearchSchema(BaseModel):
    """Input schema for the `search` tool."""

    prefix: str = Field(default="", description="Path prefix to list under. Empty = workspace root.")
    pattern: str | None = Field(
        default=None,
        description="Optional file-name glob, e.g. '*.md'. Prefix with '**/' to recurse.",
    )


_READ_FILE_TOOL_DESCRIPTION_TEMPLATE = """Reads a file from the filesystem. Assume any path the user provides is valid; reading a missing file returns an error.

Usage:
- {first_line}. Use `offset`/`limit` to page through large files instead of reading them whole.
- Results are returned with line numbers starting at `offset` + 1 (1 by default), then two spaces, then the source line. Never include these line-number prefixes when editing.
- Lines over 5,000 characters are split with continuation markers (e.g. 5.1, 5.2); `limit` counts source lines, so continuation rows do not consume the budget.
- Speculatively batch multiple `read_file` calls in one response when several files may be useful.
- An empty file returns a system-reminder warning in place of contents.
- Large tool results may be offloaded to a file; the tool message gives the path. Read that path here, paging with `offset`/`limit`.
- Images (`.png`, `.jpg`, etc.), audio, video, and PDFs return multimodal content blocks ().
{multimodal_bullets}
- Always read a file before editing it."""
"""Shared `read_file` description body for the text-only and video-aware variants.

The two variants differ only in the `{first_line}` and `{multimodal_bullets}`
fields, kept in a single template so the common guidance cannot drift between
them.
"""

_IMAGE_PDF_PAGINATION_BULLET = "- For images and PDFs, pagination via `offset`/`limit` is text-only - supply `file_path` only"
"""Multimodal bullet shared by both `read_file` descriptions (images/PDFs are not paginated)."""

READ_FILE_TOOL_DESCRIPTION = _READ_FILE_TOOL_DESCRIPTION_TEMPLATE.format(
    first_line="By default, it reads up to 100 lines starting from the beginning of the file",
    multimodal_bullets=_IMAGE_PDF_PAGINATION_BULLET,
)


EDIT_FILE_TOOL_DESCRIPTION = """Performs exact string replacements in files.

Usage:
- You must read the file before editing; this tool errors otherwise.
- Preserve the exact indentation from the read output, and never include line-number prefixes in old_string or new_string.
- Prefer editing an existing file over creating a new one.
- Only use emojis if the user explicitly requests it."""


WRITE_FILE_TOOL_DESCRIPTION = """Writes content to a file. Creates the file if it does not exist; replaces it entirely if it does.

Usage:
- Use this tool when you intend to create a new file or replace the whole file. You do not need to read the file first.
- Prefer to edit existing files (with the edit_file tool) over creating new ones when possible.
"""

DELETE_TOOL_DESCRIPTION = """Deletes a file or directory from the filesystem.

Usage:
- Permanently removes the file or directory at the given absolute path.
- Deleting a directory removes it and everything inside it, recursively. Prefer
  deleting a directory in one call over deleting each file individually.
- This cannot be undone, so only delete paths you are sure are no longer needed.
"""


FsToolName = Literal["read_file", "write_file", "edit_file", "delete", "search"]
"""Names of the built-in filesystem tools that can be passed to `FilesystemMiddleware(tools=...)`."""

_FS_TOOL_ORDER: tuple[str, ...] = ("read_file", "write_file", "edit_file", "delete", "search")
_ALL_FS_TOOL_NAMES: frozenset[str] = frozenset(_FS_TOOL_ORDER)


def _supports_delete(filesystem: object) -> bool:
    """filesystem 是否提供 delete。

    ★ FilesystemProtocol 把 delete 列为必备，所以这里恒为真 —— 保留这个
      判断只为让「未来出现只读 filesystem」时有一个现成的挂点，而不是把
      工具注册和能力判断焊死。
    """
    return callable(getattr(filesystem, "delete", None))


# Tools that should be excluded from the large result eviction logic.
#
# This tuple contains tools that should NOT have their results evicted to the filesystem
# when they exceed token limits. Tools are excluded for different reasons:
#
# 1. Tools with built-in truncation (ls, glob, grep):
#    These tools truncate their own output when it becomes too large. When these tools
#    produce truncated output due to many matches, it typically indicates the query
#    needs refinement rather than full result preservation. In such cases, the truncated
#    matches are potentially more like noise and the LLM should be prompted to narrow
#    its search criteria instead.
#
# 2. Tools with problematic truncation behavior (read_file):
#    read_file is tricky to handle as the failure mode here is single long lines
#    (e.g., imagine a jsonl file with very long payloads on each line). If we try to
#    truncate the result of read_file, the agent may then attempt to re-read the
#    truncated file using read_file again, which won't help.
#
# 3. Tools that never exceed limits (edit_file, write_file):
#    These tools return minimal confirmation messages and are never expected to produce
#    output large enough to exceed token limits, so checking them would be unnecessary.
TOOLS_EXCLUDED_FROM_EVICTION = (
    "ls",
    "glob",
    "grep",
    "read_file",
    "edit_file",
    "write_file",
    "delete",
)


TOO_LARGE_HUMAN_MSG = """Message content too large and was saved to the filesystem at: {file_path}

You can read the full content using the read_file tool with pagination (offset and limit parameters).

Here is a preview showing the head and tail of the content:

{content_sample}
"""


def _build_evicted_human_content(
    message: HumanMessage,
    replacement_text: str,
) -> str | list[ContentBlock]:
    """Build replacement content for an evicted HumanMessage, preserving non-text blocks.

    For plain string content, returns the replacement text directly. For list content
    with mixed block types (e.g., text + image), replaces all text blocks with a single
    text block containing the replacement text while keeping non-text blocks intact.

    Args:
        message: The original HumanMessage being evicted.
        replacement_text: The truncation notice and preview text.

    Returns:
        Replacement content: a string or list of content blocks.
    """
    if isinstance(message.content, str):
        return replacement_text
    media_blocks = [block for block in message.content_blocks if block["type"] != "text"]
    if not media_blocks:
        return replacement_text
    return [cast("ContentBlock", {"type": "text", "text": replacement_text}), *media_blocks]


def _build_truncated_human_message(message: HumanMessage, file_path: str) -> HumanMessage:
    """Build a truncated HumanMessage for the model request.

    Computes a preview from the full content still in state and returns a
    lightweight replacement the model will see. Pure string computation — no
    filesystem I/O.

    Args:
        message: The original HumanMessage (full content in state).
        file_path: The filesystem path where the content was evicted.

    Returns:
        A new HumanMessage with truncated content and the same `id`.
    """
    content_str = _extract_text_from_message(message)
    content_sample = _create_content_preview(content_str)
    replacement_text = TOO_LARGE_HUMAN_MSG.format(
        file_path=file_path,
        content_sample=content_sample,
    )
    evicted = _build_evicted_human_content(message, replacement_text)
    return message.model_copy(update={"content": evicted})


class FilesystemMiddleware(AgentMiddleware[AgentState, ContextT, ResponseT]):
    """Middleware for providing filesystem and optional execution tools to an agent.

    This middleware adds filesystem tools to the agent: `read_file`, `write_file`,
    `edit_file`, `delete`, and `search`.

    Files can be stored using any filesystem that implements the
    [`FilesystemProtocol`][FilesystemProtocol].

    ★ 不管 `execute`：它归 SandboxMiddleware（沙箱与文件系统是两个独立的能力）。
      这里曾按「文件系统对象是否实现 SandboxProtocol」过滤 execute —— 沙箱拆出去
      之后那段检查把 SandboxMiddleware 注册的 execute 也从模型请求里剔掉了
      （OssFilesystem 从不实现它），2026-10-19 随死代码一并删除。

    This middleware also automatically evicts large tool results to the file system when
    they exceed a token threshold, preventing context window saturation.

    Args:
        filesystem: The agent's filesystem —— a `FilesystemProtocol` instance
            (Atlas: `OssFilesystem`, the session workspace plus its `/skills`).

            `None` means no file capability: no file tools are registered and
            large-result eviction is off (there is no ephemeral fallback).

            For execution support, use a filesystem that implements
            [`SandboxProtocol`][SandboxProtocol].
        system_prompt: Optional custom system prompt override.
        tool_token_limit_before_evict: Token limit before evicting a tool result to the
            filesystem.

            When exceeded, writes the result using the configured filesystem and replaces it
            with a truncated preview and file reference.

    Example:
        ```python
        from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware
        from langchain.agents import create_agent

        # The session workspace on object storage (server: providers/filesystem)
        agent = create_agent(middleware=[FilesystemMiddleware(filesystem=oss_filesystem)])

        # No file capability: the middleware stays (it is scaffolding), no file tools
        agent = create_agent(middleware=[FilesystemMiddleware(filesystem=None)])
        ```
    """

    def __init__(
        self,
        *,
        filesystem: Any = None,
        system_prompt: str | None = None,
        tool_token_limit_before_evict: int | None = 20000,
        human_message_token_limit_before_evict: int | None = 50000,
        tools: list[FsToolName] | Literal["all"] | None = None,
        _permissions: list[FilesystemPermission] | None = None,
    ) -> None:
        """Initialize the filesystem middleware.

        Args:
            filesystem: The agent's filesystem (`FilesystemProtocol`). `None` =
                no file capability: no file tools, large-result eviction off.
            system_prompt: Optional custom system prompt override.
            tool_token_limit_before_evict: Optional token limit before evicting a tool result to the filesystem.
            human_message_token_limit_before_evict: Optional token limit before
                evicting a HumanMessage to the filesystem.
            tools: Allowlist of tool names to expose to the model.
                ``"all"` indicates all tools. If unset, defaults to `"all"`.
                Pass a list containing any of `"read_file"`, `"write_file"`,
                `"edit_file"`, `"delete"`, `"search"` to restrict the model to
                only those tools; all others are hidden. `read_file` must be
                included in any non-empty list. The capability check for
                `delete` still applies; listing it when the filesystem does not
                support it is a no-op.
            _permissions: Optional filesystem permission rules enforced directly
                by this middleware's tool implementations.

                Marked private for now because this is an internal
                implementation detail and may move to the filesystem layer in a
                future change.
        """
        if filesystem is None or (isinstance(tools, list) and tools == []):
            # ★ Atlas 改动（Sandbox S0）：空白名单 = 完全关闭文件工具。
            #   原实现没预想过这个模式（校验要求 read_file 恒在）。中间件本体
            #   必须保留 —— 它是恒装的脚手架；但大结果外置
            #   依赖 read_file 把内容读回，零工具时外置出去的内容对模型
            #   不可达，所以一并关闭。
            #
            # ★ filesystem is None 走同一条路：没有配置对象存储 = 没有文件能力。
            #   不再像原实现那样回落到内存里的临时存储 —— 那会让模型以为自己有持久
            #   工作区，写进去的东西 run 结束即弃，而它收不到任何提示。
            tools = []
            tool_token_limit_before_evict = None
            human_message_token_limit_before_evict = None
        elif isinstance(tools, list) and "read_file" not in tools:
            msg = "read_file must be included in tools; it is required by FilesystemMiddleware"
            raise ValueError(msg)
        #: None = 没有文件能力（见上方 tools 的处理）。
        self.filesystem = filesystem
        if self.filesystem is not None and callable(self.filesystem) and not isinstance(self.filesystem, FilesystemProtocol):
            msg = "filesystem must be a FilesystemProtocol instance, not a factory"
            raise TypeError(msg)

        artifacts_root = "/"
        _root = artifacts_root.rstrip("/")
        self._large_tool_results_prefix = f"{_root}/large_tool_results"
        self._conversation_history_prefix = f"{_root}/conversation_history"

        # Store configuration (private - internal implementation details)
        self._custom_system_prompt = system_prompt
        self._tool_token_limit_before_evict = tool_token_limit_before_evict
        self._human_message_token_limit_before_evict = human_message_token_limit_before_evict
        if isinstance(tools, list):
            self._enabled_tools: frozenset[str] | None = frozenset(tools)
        elif tools == "all":
            self._enabled_tools = frozenset(_ALL_FS_TOOL_NAMES)
        else:  # None -- user did not specify, defaults to all tools opted-in
            self._enabled_tools = None
        self._permissions = list(_permissions or [])

        # Shared executor for enforcing GLOB_TIMEOUT on the sync glob tool.
        # Timed-out worker threads keep running until the filesystem call returns,
        # so the semaphore rejects overload instead of queueing behind them.
        self._glob_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_SYNC_GLOB_WORKERS,
            thread_name_prefix="deepagents-glob",
        )
        self._glob_slots = threading.BoundedSemaphore(_SYNC_GLOB_WORKERS)

        tool_factories: tuple[tuple[str, Callable[[], BaseTool]], ...] = (
            ("read_file", self._create_read_file_tool),
            ("write_file", self._create_write_file_tool),
            ("edit_file", self._create_edit_file_tool),
            ("delete", self._create_delete_tool),
            ("search", self._create_search_tool),
        )
        # Excluded tools are omitted here entirely, not just hidden from the
        # model's schema, so a tool name outside `tools=` never reaches the
        # dispatchable tool node
        self.tools = [factory() for name, factory in tool_factories if self._enabled_tools is None or name in self._enabled_tools]

    def _create_search_tool(self) -> BaseTool:
        """按路径前缀与文件名列举工作区。

        ★ 不搜索内容。对象存储做不到，而工具描述必须写明并指向 execute ——
          否则模型会反复用它找内容、拿到空结果、然后据此下「没找到」的结论，
          错得很安静。
        """
        tool_description = SEARCH_TOOL_DESCRIPTION

        def _shape(result: Any, tool_call_id: str | None) -> ToolMessage:
            if result.error:
                return ToolMessage(
                    content=f"Error: {result.error}",
                    name="search",
                    tool_call_id=tool_call_id,
                    status="error",
                )
            allowed = [
                p
                for p in result.keys
                if _check_fs_permission(self._permissions, "read", validate_path(f"/{p}")) != "deny"
            ]
            lines = [*result.common_prefixes, *allowed]
            content = _format_file_paths(lines)
            if result.truncated:
                content = f"{content}\n\n[Results were capped. Narrow the prefix to see more.]"
            return ToolMessage(content=content, name="search", tool_call_id=tool_call_id)

        def sync_search(
            runtime: ToolRuntime[None, AgentState],
            prefix: str = "",
            pattern: str | None = None,
        ) -> ToolMessage:
            return _shape(self.filesystem.search(prefix, pattern=pattern), runtime.tool_call_id)

        async def async_search(
            runtime: ToolRuntime[None, AgentState],
            prefix: str = "",
            pattern: str | None = None,
        ) -> ToolMessage:
            return _shape(await self.filesystem.asearch(prefix, pattern=pattern), runtime.tool_call_id)

        return StructuredTool.from_function(
            name="search",
            description=tool_description,
            func=sync_search,
            coroutine=async_search,
            infer_schema=False,
            args_schema=SearchSchema,
        )


    def _create_read_file_tool(self) -> BaseTool:  # noqa: C901
        """Create the read_file tool."""
        tool_description = READ_FILE_TOOL_DESCRIPTION
        args_schema = ReadFileSchema
        token_limit = self._tool_token_limit_before_evict

        def _truncate(content: str, file_path: str, *, line_limit: int | None = None) -> str:
            if line_limit is not None:
                lines = content.splitlines(keepends=True)
                if len(lines) > line_limit:
                    content = "".join(lines[:line_limit])

            if token_limit and len(content) >= NUM_CHARS_PER_TOKEN * token_limit:
                truncation_msg = READ_FILE_TRUNCATION_MSG.format(file_path=file_path)
                max_content_length = NUM_CHARS_PER_TOKEN * token_limit - len(truncation_msg)
                content = content[:max_content_length] + truncation_msg

            return content

        def _handle_read_result(  # one branch per distinct read-result disposition
            read_result: ReadResult,
            validated_path: str,
            tool_call_id: str | None,
            offset: int,
            limit: int,
        ) -> ToolMessage | Command:
            if read_result.error:
                return ToolMessage(
                    content=f"Error: {read_result.error}",
                    name="read_file",
                    tool_call_id=tool_call_id,
                    status="error",
                )

            if read_result.file_data is None:
                return ToolMessage(
                    content=f"Error: no data returned for '{validated_path}'",
                    name="read_file",
                    tool_call_id=tool_call_id,
                    status="error",
                )

            file_type = _get_read_file_type(validated_path)
            encoding = read_result.file_data.get("encoding", "utf-8")
            content = read_result.file_data["content"]

            # Empty files get a uniform warning regardless of encoding/type, so
            # check before routing to avoid a degenerate empty content block for
            # binary reads.
            empty_msg = check_empty_content(content)
            if empty_msg:
                # Empty content has two causes that must not be conflated: a
                # zero-line window, where the file was never inspected, and a
                # genuinely empty file. Reporting the former as the latter
                # states something false about a file that may have contents.
                # The filesystem declares which one happened: both arrive as
                # empty content with no pagination metadata, but only the
                # never-inspected window sets `no_lines_requested` — a file
                # that was inspected and is empty (whitespace-only text from
                # `slice_read_response`'s blank branch, or empty base64 from
                # a binary read that ignored `limit`) keeps the empty-file
                # reminder.
                if not content and read_result.no_lines_requested:
                    empty_msg = NO_LINES_REQUESTED_WARNING.format(limit=limit)
                return ToolMessage(
                    content=empty_msg,
                    name="read_file",
                    tool_call_id=tool_call_id,
                    status="success",
                )

            # Route on the filesystem-declared encoding first: `"base64"` means the
            # content is binary and must never be line-numbered as text, even
            # when the extension is absent from `_EXTENSION_TO_FILE_TYPE`.
            # The extension map is only consulted to pick the multimodal block
            # type; unknown binary extensions fall back to the generic `"file"`.
            if encoding == "base64" or file_type != "text":
                block_type = file_type if file_type != "text" else "file"
                mime_type = mimetypes.guess_type("file" + Path(validated_path).suffix)[0] or "application/octet-stream"
                return ToolMessage(
                    content_blocks=cast("list[ContentBlock]", [{"type": block_type, "base64": content, "mime_type": mime_type}]),
                    name="read_file",
                    tool_call_id=tool_call_id,
                    additional_kwargs={"read_file_path": validated_path, "read_file_media_type": mime_type},
                    status="success",
                )

            # `max(offset, 0)` so the fallback gutter stays 1-indexed: a filesystem
            # that returns numberable text without `start_line` would otherwise
            # render a zero or negative line marker, which the row-marker
            # parsers downstream assume never happens.
            content = format_content_with_line_numbers(content, start_line=read_result.start_line or max(offset, 0) + 1)
            # `limit` already bounded raw source lines at the filesystem; do not
            # re-truncate by row count here, or wrapped continuation rows would
            # push real source lines off the end of the page (#2453).
            # The clamp notice is appended after truncation so it cannot be cut.
            return ToolMessage(
                content=_truncate_paginated_read(content, validated_path, read_result, token_limit) + _clamped_offset_notice(offset),
                name="read_file",
                tool_call_id=tool_call_id,
                status="success",
            )

        def sync_read_file(
            file_path: str,
            runtime: ToolRuntime[None, AgentState],
            offset: int = DEFAULT_READ_OFFSET,
            limit: int = DEFAULT_READ_LIMIT,
        ) -> ToolMessage | Command:
            """Synchronous wrapper for read_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="read_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if _check_fs_permission(self._permissions, "read", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for read on {validated_path}",
                    name="read_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            read_result = resolved_filesystem.read(validated_path, offset=offset, limit=limit)
            return _handle_read_result(read_result, validated_path, runtime.tool_call_id, offset, limit)

        async def async_read_file(
            file_path: str,
            runtime: ToolRuntime[None, AgentState],
            offset: int = DEFAULT_READ_OFFSET,
            limit: int = DEFAULT_READ_LIMIT,
        ) -> ToolMessage | Command:
            """Asynchronous wrapper for read_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="read_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            if _check_fs_permission(self._permissions, "read", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for read on {validated_path}",
                    name="read_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            read_result = await resolved_filesystem.aread(validated_path, offset=offset, limit=limit)
            return _handle_read_result(read_result, validated_path, runtime.tool_call_id, offset, limit)

        return StructuredTool.from_function(
            name="read_file",
            description=tool_description,
            func=sync_read_file,
            coroutine=async_read_file,
            infer_schema=False,
            args_schema=args_schema,
        )

    def _create_write_file_tool(self) -> BaseTool:
        """Create the write_file tool."""
        tool_description = WRITE_FILE_TOOL_DESCRIPTION

        def sync_write_file(
            file_path: str,
            content: str,
            runtime: ToolRuntime[None, AgentState],
        ) -> ToolMessage:
            """Synchronous wrapper for write_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            if _check_fs_permission(self._permissions, "write", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path}",
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: WriteResult = resolved_filesystem.write(validated_path, content)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            _emit_file_written(validated_path, len(content.encode()))
            return ToolMessage(
                content=f"Updated file {res.path}",
                name="write_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        async def async_write_file(
            file_path: str,
            content: str,
            runtime: ToolRuntime[None, AgentState],
        ) -> ToolMessage:
            """Asynchronous wrapper for write_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            if _check_fs_permission(self._permissions, "write", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path}",
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: WriteResult = await resolved_filesystem.awrite(validated_path, content)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="write_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            _emit_file_written(validated_path, len(content.encode()))
            return ToolMessage(
                content=f"Updated file {res.path}",
                name="write_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        return StructuredTool.from_function(
            name="write_file",
            description=tool_description,
            func=sync_write_file,
            coroutine=async_write_file,
            infer_schema=False,
            args_schema=WriteFileSchema,
        )

    def _create_edit_file_tool(self) -> BaseTool:
        """Create the edit_file tool."""
        tool_description = EDIT_FILE_TOOL_DESCRIPTION

        def sync_edit_file(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime[None, AgentState],
            *,
            replace_all: bool = False,
        ) -> ToolMessage:
            """Synchronous wrapper for edit_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            if _check_fs_permission(self._permissions, "write", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path}",
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: EditResult = resolved_filesystem.edit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'",
                name="edit_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        async def async_edit_file(
            file_path: str,
            old_string: str,
            new_string: str,
            runtime: ToolRuntime[None, AgentState],
            *,
            replace_all: bool = False,
        ) -> ToolMessage:
            """Asynchronous wrapper for edit_file tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            if _check_fs_permission(self._permissions, "write", validated_path) == "deny":
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path}",
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: EditResult = await resolved_filesystem.aedit(validated_path, old_string, new_string, replace_all=replace_all)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="edit_file",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=f"Successfully replaced {res.occurrences} instance(s) of the string in '{res.path}'",
                name="edit_file",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        return StructuredTool.from_function(
            name="edit_file",
            description=tool_description,
            func=sync_edit_file,
            coroutine=async_edit_file,
            infer_schema=False,
            args_schema=EditFileSchema,
        )

    def _create_delete_tool(self) -> BaseTool:  # Tool wiring + permission/support handling
        """Create the delete tool."""
        tool_description = DELETE_TOOL_DESCRIPTION

        def sync_delete(
            file_path: str,
            runtime: ToolRuntime[None, AgentState],
        ) -> ToolMessage:
            """Synchronous wrapper for delete tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            has_descendants = _delete_target_may_have_descendants(resolved_filesystem, validated_path, permissions_configured=bool(self._permissions))
            denying_patterns = _find_delete_deny_patterns(self._permissions, validated_path, has_descendants=has_descendants)
            if denying_patterns:
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path} (matches deny rule(s): {', '.join(denying_patterns)})",
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: DeleteResult = resolved_filesystem.delete(validated_path)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=f"Deleted {res.path}",
                name="delete",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        async def async_delete(
            file_path: str,
            runtime: ToolRuntime[None, AgentState],
        ) -> ToolMessage:
            """Asynchronous wrapper for delete tool."""
            resolved_filesystem = self.filesystem
            try:
                validated_path = validate_path(file_path)
            except ValueError as e:
                return ToolMessage(
                    content=f"Error: {e}",
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )

            has_descendants = await _adelete_target_may_have_descendants(
                resolved_filesystem, validated_path, permissions_configured=bool(self._permissions)
            )
            denying_patterns = _find_delete_deny_patterns(self._permissions, validated_path, has_descendants=has_descendants)
            if denying_patterns:
                return ToolMessage(
                    content=f"Error: permission denied for write on {validated_path} (matches deny rule(s): {', '.join(denying_patterns)})",
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            res: DeleteResult = await resolved_filesystem.adelete(validated_path)
            if res.error:
                return ToolMessage(
                    content=res.error,
                    name="delete",
                    tool_call_id=runtime.tool_call_id,
                    status="error",
                )
            return ToolMessage(
                content=f"Deleted {res.path}",
                name="delete",
                tool_call_id=runtime.tool_call_id,
                status="success",
            )

        return StructuredTool.from_function(
            name="delete",
            description=tool_description,
            func=sync_delete,
            coroutine=async_delete,
            infer_schema=False,
            args_schema=DeleteSchema,
        )


    @staticmethod
    def _tool_name(tool: object) -> str | None:
        """Extract a request tool name from `BaseTool`, dict, or test doubles."""
        if isinstance(tool, BaseTool):
            return tool.name
        if isinstance(tool, dict):
            return cast("str | None", cast("dict[str, Any]", tool).get("name"))
        if hasattr(tool, "name"):
            return cast("str | None", tool.name)
        get = getattr(tool, "get", None)
        if callable(get):
            return cast("str | None", get("name"))
        return None

    def _unsupported_tools(self, tool_names: set[str | None]) -> set[str | None]:
        """这个文件系统撑不起来的文件工具（目前只有 delete 一项能力门禁）。

        `tools=` 的白名单在 `__init__` 里就生效了（不在白名单里的根本不在 `self.tools`），
        这里只按文件系统的能力过滤。★ 只管自己的工具 —— 别的中间件注册的工具
        （如 SandboxMiddleware 的 execute）不归它判断。
        """
        if "delete" in tool_names and not _supports_delete(self.filesystem):
            return {"delete"}
        return set()

    def _filter_unsupported_tools_and_apply_prompt(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Drop capability-gated tools the filesystem can't serve, then apply the system prompt.

        Shared by the sync and async `wrap_model_call` paths (the only part that
        differs between them is sync vs. async message eviction). The `delete`
        tool is optional per filesystem, so when the filesystem doesn't support
        it the tool is filtered out of the request rather than advertised to the
        model and left to fail at call time.

        Returns the request with unsupported tools removed and the filesystem
        system prompt appended.
        """
        tool_names: set[str | None] = {self._tool_name(tool) for tool in request.tools}
        unsupported = self._unsupported_tools(tool_names)
        visible_tools = [tool for tool in request.tools if self._tool_name(tool) not in unsupported]
        if unsupported:
            request = request.override(tools=visible_tools)

        # ★ execute 与 grep 的描述过滤随两者一起移走：execute 归
        #   SandboxMiddleware，grep 已被 search 取代。宿主路径映射同理 ——
        #   它是给 execute 的 shell 用的配置，不属于文件工具。
        system_prompt = (self._custom_system_prompt or "").strip()

        if system_prompt:
            new_system_message = append_to_system_message(request.system_message, system_prompt)
            request = request.override(system_message=new_system_message)

        return request

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse:
        """Update the system prompt, filter tools, and evict oversized HumanMessages.

        In addition to the system-prompt and tool-filtering logic, this method
        handles large HumanMessage eviction:

        1. Any message already tagged with `lc_evicted_to` in
            `additional_kwargs` is replaced with a truncated preview for the
            model request (content in state is unchanged).
        2. If the most recent message is an untagged HumanMessage exceeding the
            eviction threshold, its content is written to the filesystem and the
            message is tagged in state via `ExtendedModelResponse`.

        It also scrubs unsupported multimodal blocks, replacing them with text
        placeholders to avoid non-retryable provider errors.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response, or an `ExtendedModelResponse` with a state
                update tagging a newly evicted message.
        """
        request = self._filter_unsupported_tools_and_apply_prompt(request)

        request_messages = _move_media_results_after_tool_results(list(request.messages))
        request_messages = _scrub_unsupported_multimodal_content(request_messages, request.model)
        if request_messages != list(request.messages):
            request = request.override(messages=request_messages)

        eviction_result = self._evict_and_truncate_messages(request)
        if eviction_result is not None:
            messages, state_command = eviction_result
            request = request.override(messages=messages)
            response = handler(request)
            if state_command is not None:
                return ExtendedModelResponse(model_response=response, command=state_command)
            return response

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse:
        """(async) Update the system prompt and filter tools based on filesystem capabilities.

        Also evicts oversized HumanMessages to the filesystem. See
        `wrap_model_call` for full documentation.

        Args:
            request: The model request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The model response from the handler, or an `ExtendedModelResponse`
                with a state update tagging newly evicted messages.
        """
        request = self._filter_unsupported_tools_and_apply_prompt(request)

        request_messages = _move_media_results_after_tool_results(list(request.messages))
        request_messages = _scrub_unsupported_multimodal_content(request_messages, request.model)
        if request_messages != list(request.messages):
            request = request.override(messages=request_messages)

        eviction_result = await self._aevict_and_truncate_messages(request)
        if eviction_result is not None:
            messages, state_command = eviction_result
            request = request.override(messages=messages)
            response = await handler(request)
            if state_command is not None:
                return ExtendedModelResponse(model_response=response, command=state_command)
            return response

        return await handler(request)

    def _process_large_message(
        self,
        message: ToolMessage,
        resolved_filesystem: FilesystemProtocol,
    ) -> tuple[ToolMessage, bool]:
        """Process a large ToolMessage by evicting its content to filesystem.

        Args:
            message: The ToolMessage with large content to evict.
            resolved_filesystem: The filesystem filesystem to write the content to.

        Returns:
            A tuple of `(processed_message, evicted)`:

                - processed_message: New `ToolMessage` with truncated content
                    and file reference
                - evicted: Whether the content was evicted to the filesystem

        !!! note

            Text is extracted from all text content blocks, joined, and used for
            both the size check and eviction. Non-text blocks
            (images, audio, etc.) are preserved in the replacement message
            so multimodal context is not lost. The model can recover
            the full text by reading the offloaded file from the filesystem.
        """
        # Early exit if eviction not configured
        if not self._tool_token_limit_before_evict:
            return message, False

        content_str = _extract_text_from_message(message)

        # Check if content exceeds eviction threshold
        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, False

        processed_message = _offload_tool_message_content(
            message,
            content_str,
            resolved_filesystem,
            self._large_tool_results_prefix,
        )
        if processed_message is None:
            return message, False
        return processed_message, True

    async def _aprocess_large_message(
        self,
        message: ToolMessage,
        resolved_filesystem: FilesystemProtocol,
    ) -> tuple[ToolMessage, bool]:
        """Async version of _process_large_message.

        Uses async filesystem methods to avoid sync calls in async context.

        See `_process_large_message` for full documentation.
        """
        # Early exit if eviction not configured
        if not self._tool_token_limit_before_evict:
            return message, False

        content_str = _extract_text_from_message(message)

        if len(content_str) <= NUM_CHARS_PER_TOKEN * self._tool_token_limit_before_evict:
            return message, False

        processed_message = await _aoffload_tool_message_content(
            message,
            content_str,
            resolved_filesystem,
            self._large_tool_results_prefix,
        )
        if processed_message is None:
            return message, False
        return processed_message, True

    def _check_eviction_needed(
        self,
        messages: list[AnyMessage],
    ) -> tuple[bool, bool]:
        """Check whether any message processing is needed.

        Args:
            messages: The message list to inspect.

        Returns:
            Tuple of `(has_tagged, new_eviction_needed)`.
        """
        if not self._human_message_token_limit_before_evict:
            return False, False

        threshold = NUM_CHARS_PER_TOKEN * self._human_message_token_limit_before_evict
        has_tagged = any(isinstance(msg, HumanMessage) and msg.additional_kwargs.get("lc_evicted_to") for msg in messages)
        new_eviction_needed = False
        if messages and isinstance(messages[-1], HumanMessage):
            last = messages[-1]
            if not last.additional_kwargs.get("lc_evicted_to") and len(_extract_text_from_message(last)) > threshold:
                new_eviction_needed = True
        return has_tagged, new_eviction_needed

    @staticmethod
    def _apply_eviction_and_truncate(
        messages: list[AnyMessage],
        write_result: WriteResult | None,
        file_path: str | None,
    ) -> tuple[list[AnyMessage], Command | None]:
        """Tag a newly evicted message and truncate all tagged messages.

        When a new eviction fires, emits a `Command` whose messages update
        contains only the tagged `HumanMessage`. The original already carries an ID in
        state (`add_messages` assigns one on write), so the tagged copy (which
        reuses that ID) replaces it in place — no `REMOVE_ALL_MESSAGES` sentinel
        is needed.
        Using a sentinel would also clobber the `AIMessage` that the model node
        writes in the same super-step.

        Args:
            messages: The message list (may be modified if write succeeded).
            write_result: Result of the filesystem write, or `None` if no new
                eviction was attempted.
            file_path: Path the content was written to.

        Returns:
            Tuple of `(processed_messages, state_command)`.
        """
        state_command: Command | None = None

        if write_result is not None and file_path is not None and not write_result.error:
            last = messages[-1]
            tagged = last.model_copy(
                update={
                    "id": last.id if last.id is not None else str(uuid.uuid4()),
                    "additional_kwargs": {
                        **last.additional_kwargs,
                        "lc_evicted_to": file_path,
                    },
                }
            )
            state_command = Command(update={"messages": [tagged]})
            messages = [*messages[:-1], tagged]

        processed: list[AnyMessage] = []
        for msg in messages:
            if isinstance(msg, HumanMessage) and msg.additional_kwargs.get("lc_evicted_to"):
                processed.append(_build_truncated_human_message(msg, msg.additional_kwargs["lc_evicted_to"]))
            else:
                processed.append(msg)

        return processed, state_command

    def _evict_and_truncate_messages(
        self,
        request: ModelRequest[ContextT],
    ) -> tuple[list[AnyMessage], Command | None] | None:
        """Evict a new oversized `HumanMessage` and truncate all tagged messages.

        Returns `None` if no messages needed processing (fast path). Otherwise
        returns `(processed_messages, command)` where `command` is a state
        update tagging the newly evicted message, or `None` if only
        previously-tagged messages were truncated.

        Args:
            request: The model request being processed.

        Returns:
            Tuple of `(messages, command)` if any processing occurred, else `None`.
        """
        messages = list(request.messages)
        has_tagged, new_eviction_needed = self._check_eviction_needed(messages)
        if not has_tagged and not new_eviction_needed:
            return None

        write_result: WriteResult | None = None
        file_path: str | None = None
        if new_eviction_needed:
            filesystem = self.filesystem
            file_path = f"{self._conversation_history_prefix}/{uuid.uuid4()}.md"
            write_result = filesystem.write(file_path, _extract_text_from_message(messages[-1]))

        return self._apply_eviction_and_truncate(messages, write_result, file_path)

    async def _aevict_and_truncate_messages(
        self,
        request: ModelRequest[ContextT],
    ) -> tuple[list[AnyMessage], Command | None] | None:
        """Async version of `_evict_and_truncate_messages`.

        Args:
            request: The model request being processed.

        Returns:
            Tuple of `(messages, command)` if any processing occurred, else `None`.
        """
        messages = list(request.messages)
        has_tagged, new_eviction_needed = self._check_eviction_needed(messages)
        if not has_tagged and not new_eviction_needed:
            return None

        write_result: WriteResult | None = None
        file_path: str | None = None
        if new_eviction_needed:
            filesystem = self.filesystem
            file_path = f"{self._conversation_history_prefix}/{uuid.uuid4()}.md"
            write_result = await filesystem.awrite(file_path, _extract_text_from_message(messages[-1]))

        return self._apply_eviction_and_truncate(messages, write_result, file_path)

    @staticmethod
    def _unwrap_command_messages(update: Mapping[str, Any]) -> tuple[Any, bool]:
        """Return the message list from a Command update and whether it was prefixed with a `REMOVE_ALL_MESSAGES` sentinel.

        Tools that want to atomically replace the messages channel emit
        `[RemoveMessage(REMOVE_ALL_MESSAGES), *messages]`. Detect that
        sentinel so we can preserve it after processing.
        """
        command_messages = update.get("messages", [])
        if (
            isinstance(command_messages, list)
            and command_messages
            and isinstance(command_messages[0], RemoveMessage)
            and command_messages[0].id == REMOVE_ALL_MESSAGES
        ):
            return command_messages[1:], True
        return command_messages, False

    @staticmethod
    def _rewrap_command_messages(messages: list[AnyMessage], *, wrapped: bool) -> list[AnyMessage | RemoveMessage]:
        """Restore the `REMOVE_ALL_MESSAGES` sentinel when the original update used one."""
        if wrapped:
            return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
        return list(messages)

    def _intercept_large_tool_result(self, tool_result: ToolMessage | Command) -> ToolMessage | Command:
        """Intercept and process large tool results before they're added to state.

        Args:
            tool_result: The tool result to potentially evict (`ToolMessage` or `Command`).

        Returns:
            Either the original result (if small enough) or a processed result with
                evicted content written to filesystem and truncated message.

        !!! note

            Handles both single `ToolMessage` results and `Command` objects
            containing multiple messages. Large content is automatically
            offloaded to filesystem to prevent context window overflow.
        """
        if isinstance(tool_result, ToolMessage):
            resolved_filesystem = self.filesystem
            processed_message, _evicted = self._process_large_message(
                tool_result,
                resolved_filesystem,
            )
            return processed_message

        if isinstance(tool_result, Command):
            update = tool_result.update
            if update is None:
                return tool_result
            command_messages, wrapped = self._unwrap_command_messages(update)
            resolved_filesystem = self.filesystem
            processed_messages = []
            for message in command_messages:
                if not isinstance(message, ToolMessage):
                    processed_messages.append(message)
                    continue

                processed_message, _evicted = self._process_large_message(
                    message,
                    resolved_filesystem,
                )
                processed_messages.append(processed_message)
            new_messages = self._rewrap_command_messages(processed_messages, wrapped=wrapped)
            return Command(
                goto=tool_result.goto,
                graph=tool_result.graph,
                update={**update, "messages": new_messages},
            )
        msg = f"Unreachable code reached in _intercept_large_tool_result: for tool_result of type {type(tool_result)}"
        raise AssertionError(msg)

    async def _aintercept_large_tool_result(self, tool_result: ToolMessage | Command) -> ToolMessage | Command:
        """Async version of _intercept_large_tool_result.

        Uses async filesystem methods to avoid sync calls in async context.

        See `_intercept_large_tool_result` for full documentation.
        """
        if isinstance(tool_result, ToolMessage):
            resolved_filesystem = self.filesystem
            processed_message, _evicted = await self._aprocess_large_message(
                tool_result,
                resolved_filesystem,
            )
            return processed_message

        if isinstance(tool_result, Command):
            update = tool_result.update
            if update is None:
                return tool_result
            command_messages, wrapped = self._unwrap_command_messages(update)
            resolved_filesystem = self.filesystem
            processed_messages = []
            for message in command_messages:
                if not isinstance(message, ToolMessage):
                    processed_messages.append(message)
                    continue

                processed_message, _evicted = await self._aprocess_large_message(
                    message,
                    resolved_filesystem,
                )
                processed_messages.append(processed_message)
            new_messages = self._rewrap_command_messages(processed_messages, wrapped=wrapped)
            return Command(
                goto=tool_result.goto,
                graph=tool_result.graph,
                update={**update, "messages": new_messages},
            )
        msg = f"Unreachable code reached in _aintercept_large_tool_result: for tool_result of type {type(tool_result)}"
        raise AssertionError(msg)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Check the size of the tool call result and evict to filesystem if too large.

        Args:
            request: The tool call request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The raw `ToolMessage`, or a pseudo tool message with the `ToolResult` in state.

        !!! note

            Tool-execution exceptions (including `ToolException`) propagate
            through this wrapper unhandled by design.
        """
        tool_result = handler(request)

        if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
            return tool_result

        return self._intercept_large_tool_result(tool_result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """(async) Check the size of the tool call result and evict to filesystem if too large.

        Args:
            request: The tool call request being processed.
            handler: The handler function to call with the modified request.

        Returns:
            The raw `ToolMessage`, or a pseudo tool message with the `ToolResult` in state.

        Note:
            Tool-execution exceptions (including `ToolException`) propagate
                through this wrapper unhandled by design.
        """
        tool_result = await handler(request)

        if self._tool_token_limit_before_evict is None or request.tool_call["name"] in TOOLS_EXCLUDED_FROM_EVICTION:
            return tool_result

        return await self._aintercept_large_tool_result(tool_result)
