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
from .models import Document, text_hash
from .splitter import FixedRecursiveCharacterTextSplitter
from .structure import StructureOptions, build_parents, child_texts, parse_sections

__all__ = [
    "AUTOMATIC_RULES",
    "HIERARCHICAL_DEFAULT_RULES",
    "STRUCTURE_TEXT_DEFAULTS",
    "normalize_process_rule",
    "transform",
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
    return {"max_heading_level": level, "max_length": size, "min_length": small}


def normalize_process_rule(
    mode: str, rules: dict[str, Any] | None, *, doc_form: str, max_length: int
) -> tuple[str, dict[str, Any]]:
    """校验并补全处理规则，返回 (mode, rules)。doc_form 决定允许的 mode。"""
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
    cfg = rules["structure"]
    opts = StructureOptions(
        max_heading_level=cfg["max_heading_level"],
        max_length=cfg["max_length"],
        min_length=cfg["min_length"],
        child_max_length=child_max_length,
    )
    meta = dict(documents[0].metadata) if documents else {}
    meta.pop("page", None)
    out: list[Document] = []
    for p in build_parents(parse_sections(text, opts.max_heading_level), opts):
        node = Document(
            page_content=p.content,
            metadata={
                **meta,
                "doc_id": str(uuid.uuid4()),
                "doc_hash": text_hash(p.content),
                "title_path": p.path,
            },
        )
        if child_max_length:
            node.children = [
                Document(
                    page_content=text_,
                    metadata={
                        "doc_id": str(uuid.uuid4()),
                        "doc_hash": text_hash(text_),
                        "context": ctx,
                    },
                )
                for text_, ctx in child_texts(p, opts)
            ]
        out.append(node)
        if preview_limit and len(out) >= preview_limit:
            break
    return out


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
