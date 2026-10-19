"""解析器路由（doc/knowledge-enterprise-design.html §4.2）：文件 → DocIR。

    pdf                       pdf_layout   版面感知（阅读顺序、页眉页脚、标题、表格、图片）
    docx / pptx               office       结构化（标题、段落、列表、表格、图片）
    xlsx / csv                tabular      每个工作表一张表
    md / txt / html / …       text         整段原文，交给现有的文本结构识别

新增格式：在 _PARSERS 里注册一个 `(data, filename) -> DocIR` 即可。

处理规则的 parser 选项（按文档选解析器）：
    auto   按格式路由（上表）
    text   只抽纯文本（原解析器，按页）：版面分析在个别文档上出错时的退路
后续的高级解析器（Docling 等）也作为这里的一个选项接入。
"""

from __future__ import annotations

from collections.abc import Callable

from ...errors import Invalid
from ..docir import Block, DocIR
from ..extractor import SUPPORTED_EXTENSIONS as _LEGACY
from ..extractor import extension_of, extract
from . import office, pdf_layout, tabular, text

__all__ = ["PARSER_OPTIONS", "SUPPORTED_EXTENSIONS", "parse"]

PARSER_OPTIONS = ("auto", "text")

Parser = Callable[[bytes, str], DocIR]

_PARSERS: dict[str, Parser] = {
    "pdf": pdf_layout.parse,
    "docx": office.parse_docx,
    "pptx": office.parse_pptx,
    "xlsx": tabular.parse_xlsx,
    "csv": tabular.parse_csv,
    "html": text.parse_html,
    "htm": text.parse_html,
}
for _ext in _LEGACY:
    _PARSERS.setdefault(_ext, text.parse_text)

SUPPORTED_EXTENSIONS = frozenset(_PARSERS)


def _plain(data: bytes, filename: str) -> DocIR:
    """原解析器：每页一个 text 块（保留页码），分段走文本结构识别。"""
    blocks = [
        Block(type="text", text=d.page_content, page=d.metadata.get("page"))
        for d in extract(filename, data)
        if d.page_content.strip()
    ]
    return DocIR(blocks=blocks, meta={"parser": "text"}, report={"parser": "text"})


def parse(filename: str, data: bytes, parser: str = "auto") -> DocIR:
    ext = extension_of(filename)
    impl = _PARSERS.get(ext)
    if impl is None:
        raise Invalid(f"不支持的文件类型：.{ext}", supported=sorted(SUPPORTED_EXTENSIONS))
    if parser == "text" and ext in _LEGACY:
        impl = _plain
    ir = impl(data, filename)
    ir.meta.setdefault("source", filename)
    ir.meta.setdefault("format", ext)
    return ir
