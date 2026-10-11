"""解析器（同 Dify core/rag/extractor/，内置解析器的库选择一致）。

    pdf              pypdfium2，按页
    docx             python-docx，段落 + 表格（表格转 Markdown）
    xlsx             openpyxl，每行转成「列名: 值; …」
    csv              每行转成「列名: 值; …」
    md / markdown    原文
    html / htm       BeautifulSoup 取正文
    txt 等纯文本      charset-normalizer 识别编码

入口 `extract(filename, data)` 返回若干 Document（一页 / 一个工作表 / 整篇）。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ...errors import Invalid
from ..models import Document
from . import builtin

__all__ = ["SUPPORTED_EXTENSIONS", "extension_of", "extract"]

_HANDLERS = {
    "pdf": builtin.extract_pdf,
    "docx": builtin.extract_docx,
    "xlsx": builtin.extract_xlsx,
    "csv": builtin.extract_csv,
    "md": builtin.extract_text,
    "markdown": builtin.extract_text,
    "mdx": builtin.extract_text,
    "txt": builtin.extract_text,
    "text": builtin.extract_text,
    "json": builtin.extract_text,
    "yaml": builtin.extract_text,
    "yml": builtin.extract_text,
    "html": builtin.extract_html,
    "htm": builtin.extract_html,
}

SUPPORTED_EXTENSIONS = frozenset(_HANDLERS)


def extension_of(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower().lstrip(".")


def extract(filename: str, data: bytes) -> list[Document]:
    ext = extension_of(filename)
    handler = _HANDLERS.get(ext)
    if handler is None:
        raise Invalid(f"不支持的文件类型：.{ext}", supported=sorted(SUPPORTED_EXTENSIONS))
    docs = handler(data)
    for doc in docs:
        doc.metadata.setdefault("source", filename)
    return [d for d in docs if d.page_content.strip()]
