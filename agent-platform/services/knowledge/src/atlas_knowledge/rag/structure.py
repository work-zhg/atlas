"""结构感知分段：按标题切小节，小节内按结构单元切子块。

算法借鉴 Docling 的 HierarchicalChunker / HybridChunker（docling-core，MIT），中文编号标题的识别
借鉴 RAGFlow 手册模板的正则规则组：

  ① 解析：文本 → 单元（标题 / 段落 / 列表项 / 表格行 / 代码块）
  ② 组装：维护标题栈，每个标题管辖到下一个同级或更高级标题为止，成为一个小节，带标题路径；
     只有标题没有正文的小节不单独成块（它的标题留在下级小节的路径里）；
     比层级上限更深的标题当作正文单元
  ③ 父块：一个小节一个父块；超长的在小节内按单元边界拆开，每块都带同样的标题路径；
     同一上级下相邻的短小节合并（只合并完整的小节）
  ④ 子块：父块里的单元；太短的并入下一个，太长的按句切开；
     建向量时的前缀 = 标题路径（表格行再加表头），存成 context —— 子块正文仍是原文

★ 一个标题都识别不出来时，整篇是一个无标题小节，③ 会按单元边界打包 —— 自然退回按段落切。
★ 标题层级按文档里实际出现的级别排名：只用了 ## 和 ### 的文档，它们就是第 1、2 级。
★ --- 这类分隔线是装饰，不作为单元。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .splitter import FixedRecursiveCharacterTextSplitter

__all__ = [
    "Parent",
    "Section",
    "StructureOptions",
    "Unit",
    "build_parents",
    "child_texts",
    "parse_sections",
    "path_text",
]

PATH_SEP = " > "

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)、])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CN_NUM = "零一二三四五六七八九十百千0-9"
#: 中文编号标题的规则组（RAGFlow 手册模板同款思路）：每组按层级从高到低排列，
#: 选匹配行最多的一组。只对「短、不以句末标点结尾」的行生效，避免把正文当标题。
_CN_FAMILIES: list[list[re.Pattern[str]]] = [
    [
        re.compile(rf"^第[{_CN_NUM}]+(?:编|部分)"),
        re.compile(rf"^第[{_CN_NUM}]+章"),
        re.compile(rf"^第[{_CN_NUM}]+节"),
        re.compile(rf"^第[{_CN_NUM}]+条"),
    ],
    [
        re.compile(rf"^第[{_CN_NUM}]+章"),
        re.compile(rf"^第[{_CN_NUM}]+节"),
        re.compile(r"^[零一二三四五六七八九十百]+、"),
        re.compile(r"^[（(][零一二三四五六七八九十百]+[)）]"),
    ],
    [
        re.compile(r"^[零一二三四五六七八九十百]+、"),
        re.compile(r"^[（(][零一二三四五六七八九十百]+[)）]"),
        re.compile(r"^\d{1,2}\.\d{1,2}(?:\s|$|[^\d.])"),
        re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?:\s|$|[^\d.])"),
    ],
]
_CN_HEADING_MAX_LEN = 50
_SENTENCE_END = tuple("。；;，,：:！!？?")


@dataclass
class StructureOptions:
    #: 切到第几级标题（按文档里实际出现的级别排名）
    max_heading_level: int = 3
    #: 父块（或通用分段的一段）的最大长度，字符
    max_length: int = 3000
    #: 小于它的相邻同级小节合并成一个父块；0 = 不合并（父子分段的默认）
    min_length: int = 0
    #: 子块最大长度；0 = 不切子块（通用分段）
    child_max_length: int = 300
    #: 短于它的单元并入下一个单元（如单独一行的「**核心能力**」）
    child_min_length: int = 20


@dataclass
class Unit:
    kind: (
        str  # heading（层级上限以下的标题）| paragraph | list_item | table_head | table_row | code
    )
    text: str
    #: 前面是否有空行（拼回父块正文时还原段落间距）
    gap: bool = False
    #: 表格行的表头（只用于建向量的前缀）
    table_header: str = ""


@dataclass
class Section:
    path: list[str]
    heading: str | None  # 标题原文行；无标题小节为 None
    units: list[Unit] = field(default_factory=list)

    @property
    def parent_path(self) -> list[str]:
        return self.path[:-1] if self.heading is not None else self.path


@dataclass
class Parent:
    path: list[str]
    content: str
    units: list[Unit]
    #: 合并进来的各小节的路径（子块用自己所在小节的路径）
    unit_paths: list[list[str]]


def path_text(path: list[str] | None) -> str:
    return PATH_SEP.join(path or [])


# ───────────────────────────────────────────── ① 解析


def _strip_md(title: str) -> str:
    """标题里的 **加粗**、`代码`、链接标记去掉，路径里只留文字。"""
    title = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), title)
    title = re.sub(r"`([^`]+)`", r"\1", title)
    title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
    return title.strip()


def _detect_cn_levels(lines: list[str]) -> dict[int, int]:
    """没有 Markdown 标题时，按中文编号规则组识别标题：行号 → 规则组内的层级（0 起）。"""
    best: dict[int, int] = {}
    in_code = False
    candidates: list[tuple[int, str]] = []
    for i, raw in enumerate(lines):
        if _FENCE.match(raw):
            in_code = not in_code
            continue
        line = raw.strip()
        if (
            in_code
            or not line
            or len(line) > _CN_HEADING_MAX_LEN
            or line.endswith(_SENTENCE_END)
            or line.startswith("|")
        ):
            continue
        candidates.append((i, line))
    for family in _CN_FAMILIES:
        found: dict[int, int] = {}
        for i, line in candidates:
            for level, pat in enumerate(family):
                if pat.match(line):
                    found[i] = level
                    break
        if len(found) > len(best):
            best = found
    return best if len(best) >= 2 else {}


def _heading_ranks(levels: dict[int, int]) -> dict[int, int]:
    """原始级别 → 文档内排名（1 起）：只用了 ## 和 ### 的文档，它们就是 1、2 级。"""
    order = sorted(set(levels.values()))
    rank = {lv: i + 1 for i, lv in enumerate(order)}
    return {i: rank[lv] for i, lv in levels.items()}


def parse_sections(text: str, max_heading_level: int = 3) -> list[Section]:
    lines = text.split("\n")

    # 标题：先找 Markdown 标题（代码块里的不算）；一个都没有再试中文编号
    md_levels: dict[int, int] = {}
    in_code = False
    for i, raw in enumerate(lines):
        if _FENCE.match(raw):
            in_code = not in_code
            continue
        if not in_code and (m := _MD_HEADING.match(raw)):
            md_levels[i] = len(m.group(1))
    if md_levels:
        ranks = _heading_ranks(md_levels)
        titles = {i: _strip_md(_MD_HEADING.match(lines[i]).group(2)) for i in ranks}  # type: ignore[union-attr]
    else:
        ranks = _heading_ranks(_detect_cn_levels(lines))
        titles = {i: _strip_md(lines[i].strip()) for i in ranks}

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(path=[], heading=None)
    sections.append(current)

    unit: Unit | None = None  # 正在累积的段落 / 列表项
    gap = False
    i = 0

    def close() -> None:
        nonlocal unit
        if unit is not None and unit.text.strip():
            current.units.append(unit)
        unit = None

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # 代码块：整块一个单元
        if _FENCE.match(line):
            close()
            fence = _FENCE.match(line).group(1)  # type: ignore[union-attr]
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i].rstrip())
                if lines[i].strip().startswith(fence):
                    i += 1
                    break
                i += 1
            current.units.append(Unit("code", "\n".join(block), gap))
            gap = False
            continue

        # 标题
        if i in ranks:
            close()
            rank = ranks[i]
            if rank <= max_heading_level:
                while stack and stack[-1][0] >= rank:
                    stack.pop()
                stack.append((rank, titles[i]))
                current = Section(path=[t for _, t in stack], heading=line.strip())
                sections.append(current)
            else:
                current.units.append(Unit("heading", line.strip(), gap))
            gap = False
            i += 1
            continue

        stripped = line.strip()
        if not stripped:
            close()
            gap = True
            i += 1
            continue
        if _RULE.match(stripped):
            close()
            gap = True
            i += 1
            continue

        # 表格：表头 + 分隔行 + 每行一个单元（带表头）；没有分隔行就当普通段落
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            close()
            header = stripped
            rows: list[str] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(lines[j].strip())
                j += 1
            # 表头 + 分隔行单独一个单元：进父块正文，不进子块（每行子块的前缀里带表头）
            current.units.append(Unit("table_head", f"{header}\n{lines[i + 1].strip()}", gap))
            current.units.extend(Unit("table_row", r, False, header) for r in rows)
            gap = False
            i = j
            continue

        if _LIST_ITEM.match(line):
            close()
            unit = Unit("list_item", line, gap)
        elif unit is None:
            unit = Unit("paragraph", line, gap)
        else:
            unit.text = f"{unit.text}\n{line}"
        gap = False
        i += 1
    close()

    # 只有标题、没有正文的小节不单独成块；开头的无标题小节为空时也去掉
    return [s for s in sections if s.units]


# ───────────────────────────────────────────── ③ 父块


def _join_units(units: list[Unit], heading: str | None = None) -> str:
    parts: list[str] = []
    if heading:
        parts.append(heading)
    for u in units:
        if parts:
            parts.append("\n\n" if u.gap else "\n")
        parts.append(u.text)
    return "".join(parts).strip()


def _split_oversized(u: Unit, limit: int) -> list[Unit]:
    """单个单元超过上限（超长段落 / 代码 / 表格行）：按句切开。"""
    if len(u.text) <= limit:
        return [u]
    pieces = FixedRecursiveCharacterTextSplitter(
        chunk_size=limit, chunk_overlap=0, fixed_separator="\n"
    ).split_text(u.text)
    out = [
        Unit(u.kind, p.strip(), u.gap if k == 0 else False, u.table_header)
        for k, p in enumerate(pieces)
        if p.strip()
    ]
    return out or [u]


def _pack_section(section: Section, max_length: int) -> list[Parent]:
    """一个小节 → 一个或多个父块（按单元边界装箱）。"""
    head = section.heading
    budget = max_length - (len(head) + 1 if head else 0)
    units: list[Unit] = []
    for u in section.units:
        units.extend(_split_oversized(u, max(budget, 50)))

    parents: list[Parent] = []
    window: list[Unit] = []
    size = len(head) if head else 0
    first = True
    for u in units:
        extra = len(u.text) + (2 if u.gap else 1)
        if window and size + extra > max_length:
            parents.append(
                Parent(
                    section.path,
                    _join_units(window, head if first else None),
                    window,
                    [section.path] * len(window),
                )
            )
            first = False
            window, size = [], 0
        window.append(u)
        size += extra
    if window:
        parents.append(
            Parent(
                section.path,
                _join_units(window, head if first else None),
                window,
                [section.path] * len(window),
            )
        )
    return parents


def build_parents(sections: list[Section], opts: StructureOptions) -> list[Parent]:
    parents: list[Parent] = []
    #: 上一个父块是不是一个完整的小节（没被拆开），以及它的上级路径
    prev_whole: tuple[list[str], Section] | None = None
    for sec in sections:
        packed = _pack_section(sec, opts.max_length)
        whole = len(packed) == 1
        if (
            whole
            and prev_whole is not None
            and opts.min_length > 0
            and parents
            and prev_whole[0] == sec.parent_path
            and len(parents[-1].content) < opts.min_length
            and len(packed[0].content) < opts.min_length
            and len(parents[-1].content) + 2 + len(packed[0].content) <= opts.max_length
        ):
            # 同一上级下相邻的两个短小节合并：路径取共同的上级，各自的标题行留在正文里
            last = parents[-1]
            merged_units = [*last.units, *packed[0].units]
            parents[-1] = Parent(
                path=sec.parent_path,
                content=f"{last.content}\n\n{packed[0].content}",
                units=merged_units,
                unit_paths=[*last.unit_paths, *packed[0].unit_paths],
            )
            prev_whole = (sec.parent_path, sec)
            continue
        parents.extend(packed)
        prev_whole = (sec.parent_path, sec) if whole else None
    return parents


# ───────────────────────────────────────────── ④ 子块


def child_texts(parent: Parent, opts: StructureOptions) -> list[tuple[str, str]]:
    """父块 → [(子块原文, 建向量用的前缀)]。前缀 = 子块所在小节的标题路径（表格行再加表头）。"""
    out: list[tuple[str, str]] = []
    pending: list[Unit] = []
    pending_path: list[str] = []

    def emit(units: list[Unit], path: list[str]) -> None:
        text = "\n".join(u.text for u in units).strip()
        if not text:
            return
        header = next((u.table_header for u in units if u.table_header), "")
        context = "\n".join(p for p in (path_text(path), header) if p)
        for piece in _split_oversized(Unit("paragraph", text), opts.child_max_length):
            out.append((piece.text, context))

    for u, path in zip(parent.units, parent.unit_paths, strict=True):
        if u.kind == "table_head":
            continue
        if pending and (path != pending_path or u.table_header != pending[-1].table_header):
            emit(pending, pending_path)
            pending = []
        pending.append(u)
        pending_path = path
        if len("\n".join(x.text for x in pending)) >= opts.child_min_length:
            emit(pending, pending_path)
            pending = []
    if pending:
        if (
            out
            and len("\n".join(x.text for x in pending)) < opts.child_min_length
            and pending_path == (parent.unit_paths[-1] if parent.unit_paths else [])
        ):
            # 末尾太短的残片并进上一个子块（同一小节内）
            text, ctx = out[-1]
            out[-1] = (f"{text}\n" + "\n".join(x.text for x in pending), ctx)
        else:
            emit(pending, pending_path)
    return out
