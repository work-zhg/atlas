"""文档的统一中间表示 DocIR（doc/knowledge-enterprise-design.html §4.1）。

解析器把各种格式统一成「带类型、层级、页码与坐标的块序列」，分段直接消费结构，不再从纯文本里
猜标题和表格；页码与坐标一路带到分段上（source_locator），用于引用定位。

    Block.type   heading | paragraph | list_item | table | code | image | caption | text
                 text = 纯文本类格式（txt / md / html）的整段原文：它们没有页码与坐标可保留，
                 分段时交给现有的文本结构识别（Markdown 标题、中文编号标题、表格、代码块）

★ 图片的二进制不进 DocIR 的 JSON：解析时暂存在 DocIR.images，入库时写对象存储，
  块上只留对象存储里的 key（asset）。
★ 纯数据结构，不碰 IO。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Document

__all__ = ["Block", "DocIR", "table_markdown", "table_row_texts"]

BLOCK_TYPES = frozenset(
    {"heading", "paragraph", "list_item", "table", "code", "image", "caption", "text"}
)


@dataclass
class Block:
    type: str
    text: str = ""
    #: heading 的层级（1 起，数字越小越高）
    level: int | None = None
    #: table：{"rows": [[str]], "header_rows": int}
    table: dict[str, Any] | None = None
    #: 来源页码（1 起）；没有分页概念的格式为 None
    page: int | None = None
    #: 页内坐标 [x0, top, x1, bottom]（PDF 点，左上为原点）
    bbox: list[float] | None = None
    #: image：对象存储里的 key（入库时填）；解析时用 attrs["image_id"] 关联 DocIR.images
    asset: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocIR:
    blocks: list[Block] = field(default_factory=list)
    #: parser、page_count、title 等
    meta: dict[str, Any] = field(default_factory=dict)
    #: 解析质量（§4.7）：指标与警告
    report: dict[str, Any] = field(default_factory=dict)
    #: image_id → (二进制, MIME)。不序列化
    images: dict[str, tuple[bytes, str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ 序列化

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "meta": self.meta,
            "report": self.report,
            "blocks": [
                {k: v for k, v in asdict(b).items() if v not in (None, {}, "")} for b in self.blocks
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DocIR:
        blocks = [
            Block(
                type=b["type"],
                text=b.get("text", ""),
                level=b.get("level"),
                table=b.get("table"),
                page=b.get("page"),
                bbox=b.get("bbox"),
                asset=b.get("asset"),
                attrs=b.get("attrs") or {},
            )
            for b in data.get("blocks") or []
        ]
        return cls(blocks=blocks, meta=data.get("meta") or {}, report=data.get("report") or {})

    # ------------------------------------------------------------------ 渲染

    @property
    def is_plain_text(self) -> bool:
        """纯文本类格式：只有 text 块（交给现有的文本结构识别）。"""
        return bool(self.blocks) and all(b.type == "text" for b in self.blocks)

    def plain_text(self) -> str:
        return "\n\n".join(render_block(b) for b in self.blocks if render_block(b))

    def page_documents(self, source: str = "") -> list[Document]:
        """不按结构分段（普通分段 / 父子分段的段落、整篇模式）时的输入：一页一个 Document。

        ★ 表格渲染成「"列名":"值";…」一行一条：按长度切分时比 Markdown 表格好切，
          而且每一行都自带列名（与原先 XLSX / CSV 解析器的输出一致）。
        """
        pages: dict[int | None, list[str]] = {}
        for b in self.blocks:
            if b.type == "caption" and b.attrs.get("attached"):
                continue
            # 标题只出文字：按长度切分时 Markdown 的 # 号是噪音
            text = b.text.strip() if b.type == "heading" else render_block(b, table_as_rows=True)
            if text:
                pages.setdefault(b.page, []).append(text)
        out: list[Document] = []
        for page, parts in pages.items():
            meta: dict[str, Any] = {"source": source}
            if page is not None:
                meta["page"] = page
            out.append(Document(page_content="\n\n".join(parts), metadata=meta))
        return out


def table_markdown(rows: list[list[str]], header_rows: int = 1) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    head = norm[0] if header_rows else [f"列{i + 1}" for i in range(width)]
    body = norm[1:] if header_rows else norm
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def table_row_texts(rows: list[list[str]], header_rows: int = 1) -> list[str]:
    """表格 → 每行一条「"列名":"值";…」（空单元格省略）。"""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    header = (
        [_cell(h) or f"列{i + 1}" for i, h in enumerate(rows[0] + [""] * (width - len(rows[0])))]
        if header_rows
        else [f"列{i + 1}" for i in range(width)]
    )
    out = []
    for r in rows[1:] if header_rows else rows:
        # 格式与原 XLSX / CSV 解析器一致（同 Dify 的 Excel 处理）："列名":"值";…
        pairs = [f'"{h}":"{_cell(v)}"' for h, v in zip(header, r, strict=False) if _cell(v)]
        if pairs:
            out.append(";".join(pairs))
    return out


def render_block(b: Block, *, table_as_rows: bool = False) -> str:
    if b.type == "table" and b.table:
        rows, hr = b.table.get("rows") or [], int(b.table.get("header_rows", 1))
        return "\n".join(table_row_texts(rows, hr)) if table_as_rows else table_markdown(rows, hr)
    if b.type == "heading":
        return f"{'#' * max(1, min(b.level or 1, 6))} {b.text}"
    if b.type == "image":
        return image_text(b)
    if b.type == "code":
        return f"```\n{b.text}\n```"
    return b.text.strip()


def image_text(b: Block) -> str:
    """图片在正文里的占位：图注（没有就是「图片」）+ 资源引用，前端据此显示原图。"""
    caption = (b.attrs.get("caption") or "").strip() or "图片"
    return f"![{caption}](kb-asset:{b.asset})" if b.asset else f"[{caption}]"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", "\\|")
