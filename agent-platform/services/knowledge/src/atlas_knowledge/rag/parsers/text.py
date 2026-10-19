"""纯文本类格式（txt / md / json / yaml / html）：整段原文一个 text 块。

它们没有页码与坐标可保留；结构（Markdown 标题、中文编号标题、表格、代码块）交给现有的
文本结构识别（rag/structure.py parse_sections），行为与原解析器完全一致。
"""

from __future__ import annotations

from ..docir import Block, DocIR
from ..extractor.builtin import decode_text, extract_html

__all__ = ["parse_html", "parse_text"]


def parse_text(data: bytes, filename: str = "") -> DocIR:
    return DocIR(blocks=[Block(type="text", text=decode_text(data))], meta={"parser": "text"})


def parse_html(data: bytes, filename: str = "") -> DocIR:
    docs = extract_html(data)
    text = "\n\n".join(d.page_content for d in docs)
    return DocIR(blocks=[Block(type="text", text=text)], meta={"parser": "html"})
