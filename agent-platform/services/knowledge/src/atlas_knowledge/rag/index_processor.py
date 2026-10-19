"""索引处理器的 transform 部分（参考 Dify core/rag/index_processor/processor/）。

    text_model          ParagraphIndexProcessor    清洗 → 切分；每段一个向量
                                                   mode=structure：按标题切小节（rag/structure.py）
    hierarchical_model  ParentChildIndexProcessor  父块 → 子块；只有子块进向量库。父块切法：
                                                   structure 按标题小节（默认）/ paragraph 按规则 /
                                                   full_doc 整篇
    qa_model            （P2）

load（写向量库）在 indexing/runner.py：它要和库表、向量库一起做，放这里会让纯计算层碰 IO。

处理规则（kb_process_rule.rules，同 Dify）：
    {
      "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": true},
                               {"id": "remove_urls_emails", "enabled": false}],
      "segmentation": {"separator": "\\n\\n", "max_tokens": 500, "chunk_overlap": 50},
      "parent_mode": "structure" | "paragraph" | "full_doc",   # 仅 hierarchical
      "structure": {"max_heading_level": 3, "max_length": 3000, "min_length": 0},  # 按标题时
      "subchunk_segmentation": {"separator": "\\n", "max_tokens": 200, "chunk_overlap": 0}
    }
★ 字段名 max_tokens 沿用 Dify，但按字符计（见 splitter.py）。
★ 按标题切出的分段带 metadata.title_path（标题路径），子块带 metadata.context（建向量时的前缀：
  标题路径 + 表头）—— 存储的正文都是原文。
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from ..errors import Invalid
from .cleaner import clean
from .docir import Block, DocIR
from .models import Document, text_hash
from .splitter import FixedRecursiveCharacterTextSplitter
from .structure import (
    Embed,
    Parent,
    StructureOptions,
    build_parents,
    child_texts,
    parse_sections,
    path_text,
    sections_from_blocks,
)

__all__ = [
    "AUTOMATIC_RULES",
    "HIERARCHICAL_DEFAULT_RULES",
    "STRUCTURE_TEXT_DEFAULTS",
    "normalize_process_rule",
    "transform",
    "transform_ir",
]

#: 同 Dify DatasetProcessRule.AUTOMATIC_RULES
AUTOMATIC_RULES: dict[str, Any] = {
    "pre_processing_rules": [
        {"id": "remove_extra_spaces", "enabled": True},
        {"id": "remove_urls_emails", "enabled": False},
    ],
    "segmentation": {"separator": "\n", "max_tokens": 500, "chunk_overlap": 50},
}

HIERARCHICAL_DEFAULT_RULES: dict[str, Any] = {
    "pre_processing_rules": [
        {"id": "remove_extra_spaces", "enabled": True},
        {"id": "remove_urls_emails", "enabled": False},
    ],
    "segmentation": {"separator": "\n\n", "max_tokens": 1024, "chunk_overlap": 0},
    # ★ 默认按标题小节切父块：没有标题的文档会自动退回按段落打包。
    #   默认不合并短小节（min_length=0）：匹配靠子块，父块短一点没有坏处，
    #   而合并会让「一个项目一个父块」不成立
    "parent_mode": "structure",
    "structure": {"max_heading_level": 3, "max_length": 3000, "min_length": 0},
    "subchunk_segmentation": {"separator": "\n", "max_tokens": 300, "chunk_overlap": 0},
}

#: 通用分段按标题切时的默认值。★ 一段要整体进向量模型（bge 输入上限 512 token），上限比父块小
STRUCTURE_TEXT_DEFAULTS: dict[str, Any] = {
    "max_heading_level": 3,
    "max_length": 800,
    "min_length": 200,
}

_MIN_LENGTH = 50


def _segmentation(raw: Any, *, max_length: int, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Invalid(f"{label}缺少分段设置")
    sep = raw.get("separator", raw.get("delimiter", "\n"))
    if not isinstance(sep, str):
        raise Invalid(f"{label}分隔符必须是字符串")
    try:
        size = int(raw.get("max_tokens", 500))
        overlap = int(raw.get("chunk_overlap", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise Invalid(f"{label}分段长度必须是整数") from exc
    if size < _MIN_LENGTH or size > max_length:
        raise Invalid(f"{label}分段长度须在 {_MIN_LENGTH} ~ {max_length} 之间")
    if overlap < 0 or overlap >= size:
        raise Invalid(f"{label}重叠长度须小于分段长度")
    return {"separator": sep, "max_tokens": size, "chunk_overlap": overlap}


def _structure(raw: Any, *, defaults: dict[str, Any], max_length: int) -> dict[str, Any]:
    cfg = {**defaults, **(raw if isinstance(raw, dict) else {})}
    try:
        level = int(cfg["max_heading_level"])
        size = int(cfg["max_length"])
        small = int(cfg["min_length"])
    except (TypeError, ValueError) as exc:
        raise Invalid("按标题分段的参数必须是整数") from exc
    if not 1 <= level <= 6:
        raise Invalid("标题层级须在 1 ~ 6 之间")
    if size < 100 or size > max_length:
        raise Invalid(f"按标题分段的最大长度须在 100 ~ {max_length} 之间")
    if small < 0 or small >= size:
        raise Invalid("合并阈值须小于最大长度")
    return {
        "max_heading_level": level,
        "max_length": size,
        "min_length": small,
        # 语义分段（§5.1）：超长小节在相邻段落向量相似度最低处切开，而不是按长度硬切
        "semantic_split": bool(cfg.get("semantic_split", False)),
    }


def normalize_process_rule(
    mode: str, rules: dict[str, Any] | None, *, doc_form: str, max_length: int
) -> tuple[str, dict[str, Any]]:
    """校验并补全处理规则，返回 (mode, rules)。doc_form 决定允许的 mode。

    rules.parser（§4.2）：auto（按格式路由，默认）| text（只抽纯文本）；默认值不写进规则。
    """
    from .parsers import PARSER_OPTIONS

    parser = (rules or {}).get("parser") or "auto"
    if parser not in PARSER_OPTIONS:
        raise Invalid(f"parser 只能是 {' / '.join(PARSER_OPTIONS)}")
    mode, out = _normalize_rule(mode, rules, doc_form=doc_form, max_length=max_length)
    if parser != "auto":
        out["parser"] = parser
    return mode, out


def _normalize_rule(
    mode: str, rules: dict[str, Any] | None, *, doc_form: str, max_length: int
) -> tuple[str, dict[str, Any]]:
    if doc_form == "hierarchical_model":
        if mode != "hierarchical":
            raise Invalid("父子分段只能用 hierarchical 规则")
        merged = copy.deepcopy(HIERARCHICAL_DEFAULT_RULES)
        merged.update(rules or {})
        parent_mode = merged.get("parent_mode")
        if parent_mode not in ("structure", "paragraph", "full_doc"):
            raise Invalid("parent_mode 只能是 structure、paragraph 或 full_doc")
        out = {
            "pre_processing_rules": list(merged.get("pre_processing_rules") or []),
            "parent_mode": parent_mode,
            "subchunk_segmentation": _segmentation(
                merged.get("subchunk_segmentation"), max_length=max_length, label="子块"
            ),
        }
        if parent_mode == "paragraph":
            out["segmentation"] = _segmentation(
                merged.get("segmentation"), max_length=max_length, label="父块"
            )
        elif parent_mode == "structure":
            out["structure"] = _structure(
                merged.get("structure"),
                defaults=HIERARCHICAL_DEFAULT_RULES["structure"],
                max_length=max_length,
            )
        return mode, out
    if doc_form == "text_model":
        if mode == "automatic":
            return mode, copy.deepcopy(AUTOMATIC_RULES)
        rules = rules or {}
        if mode == "structure":
            return mode, {
                "pre_processing_rules": list(rules.get("pre_processing_rules") or []),
                "structure": _structure(
                    rules.get("structure"), defaults=STRUCTURE_TEXT_DEFAULTS, max_length=max_length
                ),
            }
        if mode != "custom":
            raise Invalid("通用分段只能用 automatic、custom 或 structure 规则")
        return mode, {
            "pre_processing_rules": list(rules.get("pre_processing_rules") or []),
            "segmentation": _segmentation(
                rules.get("segmentation"), max_length=max_length, label=""
            ),
        }
    raise Invalid(f"暂不支持的分段结构：{doc_form}")


def _splitter(seg: dict[str, Any]) -> FixedRecursiveCharacterTextSplitter:
    return FixedRecursiveCharacterTextSplitter(
        chunk_size=seg["max_tokens"],
        chunk_overlap=seg["chunk_overlap"],
        fixed_separator=seg["separator"],
    )


def _strip_leading_punct(text: str) -> str:
    """去掉切分残留在开头的句号（同 Dify）。"""
    text = text.strip()
    if text.startswith((".", "。")):
        text = text[1:].strip()
    return text


def _nodes(
    text: str, splitter: FixedRecursiveCharacterTextSplitter, meta: dict[str, Any]
) -> list[Document]:
    out: list[Document] = []
    for chunk in splitter.split_text(text):
        content = _strip_leading_punct(chunk)
        if not content:
            continue
        md = dict(meta)
        md["doc_id"] = str(uuid.uuid4())
        md["doc_hash"] = text_hash(content)
        out.append(Document(page_content=content, metadata=md))
    return out


def _structure_opts(
    rules: dict[str, Any], child_max_length: int, embed: Embed | None = None
) -> StructureOptions:
    cfg = rules["structure"]
    return StructureOptions(
        max_heading_level=cfg["max_heading_level"],
        max_length=cfg["max_length"],
        min_length=cfg["min_length"],
        child_max_length=child_max_length,
        embed=embed if cfg.get("semantic_split") else None,
    )


def _join_ctx(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p)


def _parent_nodes(
    parents: list[Parent],
    opts: StructureOptions,
    meta: dict[str, Any],
    *,
    preview_limit: int | None,
    doc_title: str = "",
) -> list[Document]:
    """父块 → 分段。向量化前缀（context）= 文档标题 + 标题路径（上下文补全 L1，§5.3）。"""
    out: list[Document] = []
    for p in parents:
        md: dict[str, Any] = {
            **meta,
            "doc_id": str(uuid.uuid4()),
            "doc_hash": text_hash(p.content),
            "title_path": p.path,
            "context": _join_ctx(doc_title, path_text(p.path)),
        }
        if (loc := p.locator()) is not None:
            md["source_locator"] = loc
        node = Document(page_content=p.content, metadata=md)
        if opts.child_max_length:
            node.children = [
                Document(
                    page_content=text_,
                    metadata={
                        "doc_id": str(uuid.uuid4()),
                        "doc_hash": text_hash(text_),
                        "context": _join_ctx(doc_title, ctx),
                    },
                )
                for text_, ctx in child_texts(p, opts)
            ]
        out.append(node)
        if preview_limit and len(out) >= preview_limit:
            break
    return out


def _structure_nodes(
    documents: list[Document],
    rules: dict[str, Any],
    *,
    child_max_length: int,
    preview_limit: int | None,
) -> list[Document]:
    """按标题小节切（rag/structure.py）。child_max_length=0 时不切子块（通用分段）。

    多页 / 多段解析结果先拼成一篇：小节可以跨页。
    """
    text = clean("\n\n".join(d.page_content for d in documents), rules)
    opts = _structure_opts(rules, child_max_length)
    meta = dict(documents[0].metadata) if documents else {}
    meta.pop("page", None)
    parents = build_parents(parse_sections(text, opts.max_heading_level), opts)
    return _parent_nodes(parents, opts, meta, preview_limit=preview_limit)


def transform(
    documents: list[Document],
    *,
    doc_form: str,
    rules: dict[str, Any],
    preview_limit: int | None = None,
) -> list[Document]:
    """解析结果 → 分段（父子模式带 children）。preview_limit：预览时最多出几段。"""
    if doc_form == "text_model" and "structure" in rules:
        return _structure_nodes(documents, rules, child_max_length=0, preview_limit=preview_limit)

    if doc_form == "text_model":
        splitter = _splitter(rules["segmentation"])
        out: list[Document] = []
        for doc in documents:
            out.extend(_nodes(clean(doc.page_content, rules), splitter, doc.metadata))
            if preview_limit and len(out) >= preview_limit:
                return out[:preview_limit]
        return out

    if doc_form == "hierarchical_model" and rules["parent_mode"] == "structure":
        return _structure_nodes(
            documents,
            rules,
            child_max_length=rules["subchunk_segmentation"]["max_tokens"],
            preview_limit=preview_limit,
        )

    if doc_form == "hierarchical_model":
        child_splitter = _splitter(rules["subchunk_segmentation"])
        if rules["parent_mode"] == "full_doc":
            content = clean("\n".join(d.page_content for d in documents), rules).strip()
            if not content:
                return []
            meta = dict(documents[0].metadata) if documents else {}
            meta.pop("page", None)
            parent = Document(
                page_content=content,
                metadata={**meta, "doc_id": str(uuid.uuid4()), "doc_hash": text_hash(content)},
            )
            parent.children = _nodes(content, child_splitter, {})
            if preview_limit:
                parent.children = parent.children[:preview_limit]
            return [parent]

        parent_splitter = _splitter(rules["segmentation"])
        parents: list[Document] = []
        for doc in documents:
            for parent in _nodes(clean(doc.page_content, rules), parent_splitter, doc.metadata):
                parent.children = _nodes(parent.page_content, child_splitter, {})
                parents.append(parent)
                if preview_limit and len(parents) >= preview_limit:
                    return parents
        return parents

    raise Invalid(f"暂不支持的分段结构：{doc_form}")


# ───────────────────────────────────────────── DocIR 入口（§4.1）


def _clean_blocks(ir: DocIR, rules: dict[str, Any]) -> list[Block]:
    """清洗规则作用到每个块的文本上；表格单元格不动（列名与数字不该被改写）。"""
    out = []
    for b in ir.blocks:
        if b.type in ("table", "image") or not b.text:
            out.append(b)
            continue
        out.append(copy.replace(b, text=clean(b.text, rules)))
    return out


def _structured(doc_form: str, rules: dict[str, Any]) -> int | None:
    """按结构分段时返回子块长度（0 = 不切子块）；不按结构分段返回 None。"""
    if doc_form == "text_model" and "structure" in rules:
        return 0
    if doc_form == "hierarchical_model" and rules.get("parent_mode") == "structure":
        return int(rules["subchunk_segmentation"]["max_tokens"])
    return None


def doc_title_of(ir: DocIR, fallback: str = "") -> str:
    """文档标题：解析器给出的标题 → 文件名去掉扩展名。"""
    title = str(ir.meta.get("title") or "").strip()
    if title:
        return title
    name = str(ir.meta.get("source") or fallback)
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def transform_ir(
    ir: DocIR,
    *,
    doc_form: str,
    rules: dict[str, Any],
    preview_limit: int | None = None,
    embed: Embed | None = None,
    doc_title: str | None = None,
) -> list[Document]:
    """DocIR → 分段。按结构分段时直接用块的结构（并带上 source_locator）；
    其余模式按页渲染成文本后走原来的切分（分段带页码）。

    embed：语义分段要用的向量化函数（处理规则开了 semantic_split 才会用到）。
    """
    source = str(ir.meta.get("source") or "")
    title = doc_title if doc_title is not None else doc_title_of(ir)
    child_len = _structured(doc_form, rules)
    if child_len is not None:
        opts = _structure_opts(rules, child_len, embed)
        if ir.is_plain_text:
            text = clean(ir.plain_text(), rules)
            sections = parse_sections(text, opts.max_heading_level)
        else:
            sections = sections_from_blocks(_clean_blocks(ir, rules), opts.max_heading_level)
        return _parent_nodes(
            build_parents(sections, opts),
            opts,
            {"source": source},
            preview_limit=preview_limit,
            doc_title=title,
        )
    documents = (
        [Document(page_content=ir.plain_text(), metadata={"source": source})]
        if ir.is_plain_text
        else ir.page_documents(source)
    )
    nodes = transform(
        [d for d in documents if d.page_content.strip()],
        doc_form=doc_form,
        rules=rules,
        preview_limit=preview_limit,
    )
    for n in nodes:
        n.metadata["context"] = title
        for c in n.children or []:
            c.metadata["context"] = _join_ctx(title, c.metadata.get("context"))
        if (page := n.metadata.get("page")) is not None:
            n.metadata.setdefault("source_locator", {"pages": [page]})
    return nodes
