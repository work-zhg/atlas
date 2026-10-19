"""PDF 版面感知解析（doc/knowledge-enterprise-design.html §4.3–4.5、§4.7）。

基于 pdfplumber 的字符坐标与字体，CPU 即可，不依赖版面模型：

  ① 逐页：词 → 行（同一高度、间隔过大的拆开 —— 双栏时左右两栏的字在同一 y 上）；
     有框线的表格（lines 策略，不用 text 策略以免把对齐的正文误判成表格）；图片
  ② 跨页：页面顶部 / 底部区域里跨页重复的文本（数字归一化后比较）= 页眉页脚，剔除；单独的页码剔除
  ③ 阅读顺序：双栏页以通栏内容为分隔分成若干段，每段先左栏后右栏；单栏按 (top, x0)
  ④ 块：行按行距、缩进、字号合并成段落；标题优先用 PDF 书签，没有书签用字号 / 加粗 / 编号打分；
     跨页续表合并；图片用 pypdfium2 按坐标裁剪渲染，并关联紧邻的「图 X」图注
  ⑤ 质量：扫描页（无文本层且被图片覆盖，不做 OCR，只标记）、乱码比例、标题 / 表格 / 图片数

★ 坐标统一为 pdfplumber 的点坐标：[x0, top, x1, bottom]，左上为原点。
"""

from __future__ import annotations

import io
import logging
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..docir import Block, DocIR
from ..structure import _detect_cn_levels

__all__ = ["parse"]

logger = logging.getLogger(__name__)

#: 页面顶部 / 底部多大比例的区域算页眉页脚候选
_EDGE = 0.08
#: 图片：小于这么大（点）的不抽；单文档最多抽多少张
_IMG_MIN_W, _IMG_MIN_H, _IMG_MAX = 60.0, 40.0, 60
_RENDER_SCALE = 2.0
_SENT_END = tuple("。；;，,：:！!？?")
_LIST_START = re.compile(
    r"^\s*(?:[-*+•●○▪◆■□➢✓]|\d{1,3}[.)、](?!\d)|[（(]\d{1,3}[)）]|[a-zA-Z][.)])\s*"
)
_PAGE_NUM = re.compile(
    r"^(?:[-–—\s]*\d+[-–—\s]*|第\s*\d+\s*页(?:\s*[/，,]?\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+|Page\s+\d+(?:\s+of\s+\d+)?)$",
    re.I,
)
_CAPTION = re.compile(r"^(?:图|Figure|Fig\.)\s*[\d一二三四五六七八九十]+[-.\d]*", re.I)
_BOLD = re.compile(r"Bold|Black|Heavy|Semibold|Demi|Hei|黑", re.I)
_MONO = re.compile(r"Mono|Courier|Consolas|Menlo", re.I)


@dataclass
class _Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool
    mono: bool
    page: int
    col: str = "F"  # L | R | F（通栏）


@dataclass
class _Item:
    """排序用：行 / 表格 / 图片统一成带坐标的项。"""

    kind: str  # line | table | image
    x0: float
    x1: float
    top: float
    bottom: float
    line: _Line | None = None
    rows: list[list[str]] | None = None
    image: dict[str, Any] | None = None
    col: str = "F"


@dataclass
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line] = field(default_factory=list)
    tables: list[_Item] = field(default_factory=list)
    images: list[_Item] = field(default_factory=list)
    chars: int = 0
    bad_chars: int = 0
    image_cover: float = 0.0
    two_column: bool = False


# ───────────────────────────────────────────── 工具


def _is_cjk(ch: str) -> bool:
    return bool(ch) and (
        "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"
    )


def _join(a: str, b: str) -> str:
    """两段文字拼接：英文行尾连字符去掉；中文边界不加空格；其余加一个空格。"""
    if not a:
        return b
    if not b:
        return a
    if a.endswith("-") and b[:1].isalpha() and not _is_cjk(b[:1]):
        return a[:-1] + b
    if _is_cjk(a[-1]) or _is_cjk(b[0]):
        return a + b
    return f"{a} {b}"


def _bad(ch: str) -> bool:
    """替换符与私用区字符：字体编码无法映射时 pdfminer 会产出它们。"""
    return ch == "\ufffd" or "\ue000" <= ch <= "\uf8ff" or unicodedata.category(ch) == "Co"


def _inside(cx: float, cy: float, box: tuple[float, float, float, float]) -> bool:
    return box[0] - 1 <= cx <= box[2] + 1 and box[1] - 1 <= cy <= box[3] + 1


# ───────────────────────────────────────────── ① 逐页


def _page_lines(words: list[dict[str, Any]], page_no: int) -> list[_Line]:
    """词 → 行。同一高度上间隔过大的词分属不同的行（双栏）。"""
    words = sorted(words, key=lambda w: (round(w["top"] / 2), w["x0"]))
    rows: list[list[dict[str, Any]]] = []
    for w in words:
        if rows:
            last = rows[-1]
            ref = last[-1]
            tol = max(2.0, 0.5 * float(ref.get("size") or 10))
            if abs(w["top"] - ref["top"]) <= tol:
                last.append(w)
                continue
        rows.append([w])
    out: list[_Line] = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        frag: list[dict[str, Any]] = []
        for w in row:
            if frag:
                size = float(frag[-1].get("size") or 10)
                if w["x0"] - frag[-1]["x1"] > max(12.0, 2.5 * size):
                    out.append(_make_line(frag, page_no))
                    frag = []
            frag.append(w)
        if frag:
            out.append(_make_line(frag, page_no))
    return out


def _make_line(words: list[dict[str, Any]], page_no: int) -> _Line:
    # ★ 词与词之间按实际间距决定是否留空格：pdfplumber 按空格切词，「第一章 总则」「图 1 组织架构」
    #   里的空格是原文的一部分；只按字符类别拼会把它们吞掉
    text = ""
    prev: dict[str, Any] | None = None
    for w in words:
        size = float(w.get("size") or 10)
        if prev is not None and w["x0"] - prev["x1"] > 0.15 * size:
            text = f"{text} {w['text']}"
        else:
            text = _join(text, w["text"]) if prev is not None else w["text"]
        prev = w
    sizes = [float(w.get("size") or 0) for w in words]
    fonts = [str(w.get("fontname") or "") for w in words]
    return _Line(
        text=text.strip(),
        x0=min(w["x0"] for w in words),
        x1=max(w["x1"] for w in words),
        top=min(w["top"] for w in words),
        bottom=max(w["bottom"] for w in words),
        size=round(statistics.median(sizes), 1) if sizes else 0.0,
        bold=sum(bool(_BOLD.search(f)) for f in fonts) * 2 > len(fonts),
        mono=sum(bool(_MONO.search(f)) for f in fonts) * 2 > len(fonts),
        page=page_no,
    )


def _analyze_page(page: Any, number: int) -> _Page:
    p = _Page(number=number, width=float(page.width), height=float(page.height))
    chars = page.chars
    p.chars = len(chars)
    p.bad_chars = sum(1 for c in chars if _bad(str(c.get("text") or "")))

    boxes: list[tuple[float, float, float, float]] = []
    try:
        found = page.find_tables(
            table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
        )
    except Exception:  # pdfplumber 对个别畸形页会抛错：当作没有表格
        found = []
    for t in found:
        rows = [[(c or "").strip() for c in r] for r in (t.extract() or [])]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        x0, top, x1, bottom = (float(v) for v in t.bbox)
        boxes.append((x0, top, x1, bottom))
        p.tables.append(_Item("table", x0, x1, top, bottom, rows=rows))

    words = page.extract_words(
        extra_attrs=["fontname", "size"], keep_blank_chars=False, x_tolerance=1.5, y_tolerance=3
    )
    words = [
        w
        for w in words
        if not any(_inside((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, b) for b in boxes)
    ]
    p.lines = [ln for ln in _page_lines(words, number) if ln.text]

    area = p.width * p.height or 1.0
    cover = 0.0
    for im in page.images:
        x0, top, x1, bottom = (float(im[k]) for k in ("x0", "top", "x1", "bottom"))
        w, h = x1 - x0, bottom - top
        cover += max(0.0, w) * max(0.0, h)
        if w < _IMG_MIN_W or h < _IMG_MIN_H:
            continue
        p.images.append(_Item("image", x0, x1, top, bottom, image={"bbox": [x0, top, x1, bottom]}))
    p.image_cover = min(1.0, cover / area)
    return p


# ───────────────────────────────────────────── ② 页眉页脚


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", "", text))


def _strip_headers_footers(pages: list[_Page]) -> list[str]:
    """剔除跨页重复的页眉页脚与单独的页码；返回被剔除的样例（报告用）。"""

    def zone(p: _Page, ln: _Line) -> bool:
        return ln.bottom <= p.height * _EDGE or ln.top >= p.height * (1 - _EDGE)

    counts: Counter[str] = Counter()
    for p in pages:
        counts.update({_norm(ln.text) for ln in p.lines if zone(p, ln)})
    need = max(2, int(0.5 * len(pages) + 0.5))
    repeated = {k for k, n in counts.items() if n >= need and len(pages) >= 2}
    removed: list[str] = []
    for p in pages:
        keep = []
        for ln in p.lines:
            if zone(p, ln) and (_norm(ln.text) in repeated or _PAGE_NUM.match(ln.text.strip())):
                if len(removed) < 10 and ln.text not in removed:
                    removed.append(ln.text)
                continue
            keep.append(ln)
        p.lines = keep
    return removed


def _strip_repeated_images(pages: list[_Page]) -> None:
    """跨页同一位置、同一尺寸的图片（logo、页眉装饰）不抽。"""

    def key(it: _Item) -> tuple[int, int, int, int]:
        return (round(it.x0 / 5), round(it.top / 5), round(it.x1 / 5), round(it.bottom / 5))

    counts: Counter[tuple[int, int, int, int]] = Counter()
    for p in pages:
        counts.update({key(it) for it in p.images})
    need = max(2, int(0.5 * len(pages) + 0.5))
    for p in pages:
        p.images = [it for it in p.images if counts[key(it)] < need or len(pages) < 2]


# ───────────────────────────────────────────── ③ 阅读顺序


def _assign_columns(p: _Page, items: list[_Item]) -> None:
    mid = p.width / 2
    for it in items:
        if it.x1 <= mid + 10:
            it.col = "L"
        elif it.x0 >= mid - 10:
            it.col = "R"
        else:
            it.col = "F"
    lines = [it for it in items if it.kind == "line"]
    left = sum(1 for it in lines if it.col == "L")
    right = sum(1 for it in lines if it.col == "R")
    p.two_column = left >= 5 and right >= 5 and min(left, right) >= 0.25 * len(lines)
    if not p.two_column:
        for it in items:
            it.col = "F"


def _reading_order(p: _Page) -> list[_Item]:
    items: list[_Item] = (
        [_Item("line", ln.x0, ln.x1, ln.top, ln.bottom, line=ln) for ln in p.lines]
        + p.tables
        + p.images
    )
    _assign_columns(p, items)
    items.sort(key=lambda it: (it.top, it.x0))
    if not p.two_column:
        return items
    ordered: list[_Item] = []
    band_l: list[_Item] = []
    band_r: list[_Item] = []

    def flush() -> None:
        ordered.extend(sorted(band_l, key=lambda it: it.top))
        ordered.extend(sorted(band_r, key=lambda it: it.top))
        band_l.clear()
        band_r.clear()

    for it in items:
        if it.col == "F":
            flush()
            ordered.append(it)
        elif it.col == "L":
            band_l.append(it)
        else:
            band_r.append(it)
    flush()
    for it in ordered:
        if it.line is not None:
            it.line.col = it.col
    return ordered


# ───────────────────────────────────────────── ④ 标题


def _body_size(pages: list[_Page]) -> float:
    weights: Counter[float] = Counter()
    for p in pages:
        for ln in p.lines:
            weights[round(ln.size * 2) / 2] += len(ln.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _toc_levels(data: bytes) -> dict[tuple[int, str], int]:
    """PDF 书签 → {(页码, 归一化标题): 层级}。"""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            out: dict[tuple[int, str], int] = {}
            for item in pdf.get_toc():
                page = getattr(item, "page_index", None)
                title = getattr(item, "title", "") or ""
                if page is None or not title.strip():
                    continue
                out[(int(page) + 1, _norm(title))] = int(getattr(item, "level", 0)) + 1
            return out
        finally:
            pdf.close()
    except Exception:
        return {}


def _heading_candidate(ln: _Line, body: float, bold_is_body: bool) -> float:
    """标题打分：0 = 不是标题；> 0 的值越大级别越高（用于分级）。"""
    text = ln.text.strip()
    if not text or len(text) > 60 or text.endswith(_SENT_END) or _LIST_START.match(text):
        return 0.0
    if ln.size >= body * 1.15:
        return ln.size
    if ln.bold and not bold_is_body and ln.size >= body * 0.95:
        return body  # 与正文同字号的加粗行：最低一级
    return 0.0


def _assign_headings(
    pages: list[_Page], ordered: dict[int, list[_Item]], toc: dict[tuple[int, str], int]
) -> dict[int, int]:
    """返回 id(_Line) → 标题层级。"""
    lines = [it.line for p in pages for it in ordered[p.number] if it.line is not None]
    levels: dict[int, int] = {}
    if len(toc) >= 3:
        hit = 0
        for ln in lines:
            key = (ln.page, _norm(ln.text))
            if key in toc:
                levels[id(ln)] = toc[key]
                hit += 1
        if hit >= 0.5 * len(toc):
            return levels
        levels.clear()

    body = _body_size(pages)
    bold_lines = sum(len(ln.text) for ln in lines if ln.bold)
    bold_is_body = bold_lines > 0.5 * max(1, sum(len(ln.text) for ln in lines))
    scores = {id(ln): _heading_candidate(ln, body, bold_is_body) for ln in lines}
    sizes = sorted({round(s * 2) / 2 for s in scores.values() if s > 0}, reverse=True)
    if sizes and sum(1 for s in scores.values() if s > 0) <= 0.4 * len(lines):
        rank = {s: i + 1 for i, s in enumerate(sizes)}
        for ln in lines:
            s = scores[id(ln)]
            if s > 0:
                levels[id(ln)] = rank[round(s * 2) / 2]
    # 同一编号模式 = 同一级别：按字号认出了「第一章 总则」，与正文同字号的「第二章 考勤」也是同级
    by_pattern: dict[str, int] = {}
    for ln in lines:
        if id(ln) in levels and (pat := _numbering(ln.text)):
            by_pattern.setdefault(pat, levels[id(ln)])
    for ln in lines:
        if (
            id(ln) not in levels
            and len(ln.text) <= 50
            and not ln.text.endswith(_SENT_END)
            and (pat := _numbering(ln.text)) in by_pattern
        ):
            levels[id(ln)] = by_pattern[pat]
    # 其余编号标题（第一章 / 一、/ 1.1）：字号与正文一样的文档靠它
    short = [ln for ln in lines if id(ln) not in levels and len(ln.text) <= 50]
    cn = _detect_cn_levels([ln.text for ln in short])
    base = max(levels.values(), default=0)
    for i, lvl in cn.items():
        levels[id(short[i])] = base + lvl + 1
    return levels


_NUMBERING = [
    ("part", re.compile(r"^第[零一二三四五六七八九十百千0-9]+(?:编|部分)")),
    ("chapter", re.compile(r"^第[零一二三四五六七八九十百千0-9]+章")),
    ("section", re.compile(r"^第[零一二三四五六七八九十百千0-9]+节")),
    ("article", re.compile(r"^第[零一二三四五六七八九十百千0-9]+条")),
    ("cn_item", re.compile(r"^[零一二三四五六七八九十百]+、")),
    ("cn_paren", re.compile(r"^[（(][零一二三四五六七八九十百]+[)）]")),
    ("d3", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?:\s|$|[^\d.])")),
    ("d2", re.compile(r"^\d{1,2}\.\d{1,2}(?:\s|$|[^\d.])")),
]


def _numbering(text: str) -> str | None:
    """标题的编号模式（第X章 / 一、/ 1.1 …）；不是编号标题返回 None。"""
    text = text.strip()
    for name, pat in _NUMBERING:
        if pat.match(text):
            return name
    return None


# ───────────────────────────────────────────── ④ 段落 / 块


def _same_paragraph(prev: _Line, cur: _Line) -> bool:
    if prev.col != cur.col or prev.page != cur.page or prev.mono != cur.mono:
        return False
    if abs(prev.size - cur.size) > 1.0 or _LIST_START.match(cur.text):
        return False
    gap = cur.top - prev.bottom
    if gap < -2 or gap > 0.8 * max(prev.size, 1.0):
        return False
    # 首行缩进 = 新段落
    if cur.x0 - prev.x0 > 1.2 * cur.size:
        return False
    # 上一行以句末标点结束、而且明显没写满一行 = 段落结束
    return not (
        prev.text.endswith(("。", "！", "？", ".", "!", "?")) and prev.x1 < cur.x1 - 2 * cur.size
    )


def _render_image(
    pdf: Any, page_no: int, page_h: float, page_w: float, bbox: list[float]
) -> bytes | None:
    try:
        page = pdf[page_no - 1]
        x0, top, x1, bottom = bbox
        crop = (max(0.0, x0), max(0.0, page_h - bottom), max(0.0, page_w - x1), max(0.0, top))
        bitmap = page.render(scale=_RENDER_SCALE, crop=crop)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        page.close()
        return buf.getvalue()
    except Exception:
        logger.debug("PDF 图片渲染失败 page=%s", page_no, exc_info=True)
        return None


def parse(data: bytes, filename: str = "") -> DocIR:
    import pdfplumber
    import pypdfium2 as pdfium

    pages: list[_Page] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            pages.append(_analyze_page(page, number))

    removed = _strip_headers_footers(pages)
    _strip_repeated_images(pages)
    ordered = {p.number: _reading_order(p) for p in pages}
    headings = _assign_headings(pages, ordered, _toc_levels(data))

    ir = DocIR(meta={"parser": "pdf_layout", "page_count": len(pages)})
    renderer = pdfium.PdfDocument(data)
    n_images = n_tables = 0
    try:
        for p in pages:
            para: Block | None = None
            para_line: _Line | None = None
            first_item = True
            for it in ordered[p.number]:
                if it.kind == "line" and it.line is not None:
                    ln = it.line
                    level = headings.get(id(ln))
                    if level is not None:
                        prev = ir.blocks[-1] if ir.blocks else None
                        # 换行的长标题：同级、同页、紧挨着 → 合并
                        if (
                            prev is not None
                            and prev.type == "heading"
                            and prev.level == level
                            and prev.page == p.number
                            and para_line is not None
                            and ln.top - para_line.bottom < 0.8 * ln.size
                        ):
                            prev.text = _join(prev.text, ln.text)
                            prev.bbox = _union(prev.bbox, ln)
                        else:
                            ir.blocks.append(
                                Block(
                                    type="heading",
                                    text=ln.text,
                                    level=level,
                                    page=p.number,
                                    bbox=_box(ln),
                                )
                            )
                        para, para_line = None, ln
                        first_item = False
                        continue
                    if (
                        para is not None
                        and para_line is not None
                        and _same_paragraph(para_line, ln)
                    ):
                        para.text = (
                            f"{para.text}\n{ln.text}" if ln.mono else _join(para.text, ln.text)
                        )
                        para.bbox = _union(para.bbox, ln)
                        para_line = ln
                        continue
                    kind = (
                        "code"
                        if ln.mono
                        else "list_item"
                        if _LIST_START.match(ln.text)
                        else "paragraph"
                    )
                    if kind == "paragraph" and _CAPTION.match(ln.text) and len(ln.text) <= 80:
                        kind = "caption"
                    para = Block(type=kind, text=ln.text, page=p.number, bbox=_box(ln))
                    ir.blocks.append(para)
                    para_line = ln
                    first_item = False
                    continue

                para, para_line = None, None
                if it.kind == "table" and it.rows:
                    prev = ir.blocks[-1] if ir.blocks else None
                    # 跨页续表：本页第一个内容就是表格，上一页以同列数的表格结束
                    if (
                        first_item
                        and prev is not None
                        and prev.type == "table"
                        and prev.page == p.number - 1
                        and prev.table
                        and len(prev.table["rows"][0]) == len(it.rows[0])
                    ):
                        rows = it.rows[1:] if it.rows[0] == prev.table["rows"][0] else it.rows
                        prev.table["rows"].extend(rows)
                        prev.attrs.setdefault("pages", [prev.page]).append(p.number)
                    else:
                        n_tables += 1
                        ir.blocks.append(
                            Block(
                                type="table",
                                table={"rows": it.rows, "header_rows": 1},
                                page=p.number,
                                bbox=[round(v, 1) for v in (it.x0, it.top, it.x1, it.bottom)],
                            )
                        )
                elif it.kind == "image" and it.image and n_images < _IMG_MAX:
                    blob = _render_image(renderer, p.number, p.height, p.width, it.image["bbox"])
                    if blob:
                        n_images += 1
                        image_id = f"img{n_images}"
                        ir.images[image_id] = (blob, "image/png")
                        ir.blocks.append(
                            Block(
                                type="image",
                                page=p.number,
                                bbox=[round(v, 1) for v in it.image["bbox"]],
                                attrs={"image_id": image_id},
                            )
                        )
                first_item = False
    finally:
        renderer.close()

    _attach_pdf_captions(ir.blocks)
    ir.report = _report(pages, ir, removed)
    return ir


def _box(ln: _Line) -> list[float]:
    return [round(ln.x0, 1), round(ln.top, 1), round(ln.x1, 1), round(ln.bottom, 1)]


def _union(bbox: list[float] | None, ln: _Line) -> list[float]:
    if not bbox:
        return _box(ln)
    return [
        round(min(bbox[0], ln.x0), 1),
        round(min(bbox[1], ln.top), 1),
        round(max(bbox[2], ln.x1), 1),
        round(max(bbox[3], ln.bottom), 1),
    ]


def _attach_pdf_captions(blocks: list[Block]) -> None:
    """图片紧邻的 caption 块 → 图片的图注。"""
    for i, b in enumerate(blocks):
        if b.type != "image":
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(blocks) and blocks[j].type == "caption" and blocks[j].page == b.page:
                b.attrs["caption"] = blocks[j].text
                # 图注已经挂到图片上：分段时跳过这一块，避免同一段文字出现两次
                blocks[j].attrs["attached"] = True
                break


# ───────────────────────────────────────────── ⑤ 质量


def _report(pages: list[_Page], ir: DocIR, removed: list[str]) -> dict[str, Any]:
    scanned = [p.number for p in pages if p.chars < 10 and p.image_cover >= 0.4]
    empty = [p.number for p in pages if p.chars < 10]
    total = sum(p.chars for p in pages) or 1
    garbled = sum(p.bad_chars for p in pages) / total
    text = "".join(b.text for b in ir.blocks)
    garbled = max(garbled, text.count("(cid:") * 5 / max(len(text), 1))
    n_head = sum(1 for b in ir.blocks if b.type == "heading")
    warnings: list[str] = []
    if pages and len(scanned) / len(pages) >= 0.2:
        warnings.append(f"疑似扫描件：{len(scanned)}/{len(pages)} 页没有文本层，需要 OCR 才能识别")
    elif empty and len(empty) == len(pages):
        warnings.append("没有提取到任何文本")
    if garbled >= 0.05:
        warnings.append(f"乱码比例 {garbled:.0%}：字体编码可能无法识别")
    # 以上是影响内容本身的问题（缺文字、乱码）：入库后先进待审核（§4.7）
    needs_review = bool(warnings)
    if len(pages) >= 5 and len(text) >= 3000 and n_head == 0:
        warnings.append("未识别出标题：按标题分段将退化为按段落切分")
    return {
        "pages": len(pages),
        "two_column_pages": sum(1 for p in pages if p.two_column),
        "scanned_pages": scanned,
        "empty_pages": empty,
        "garbled_ratio": round(garbled, 4),
        "headings": n_head,
        "tables": sum(1 for b in ir.blocks if b.type == "table"),
        "images": sum(1 for b in ir.blocks if b.type == "image"),
        "header_footer_removed": removed,
        "warnings": warnings,
        "needs_review": needs_review,
    }
