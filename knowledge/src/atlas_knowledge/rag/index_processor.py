"""索引处理器的 transform 部分（参考 Dify core/rag/index_processor/processor/）。

    text_model          ParagraphIndexProcessor    清洗 → 切分；每段一个向量
    hierarchical_model  ParentChildIndexProcessor  父块（paragraph：按规则切；full_doc：整篇）
                                                   → 子块；只有子块进向量库
    qa_model            （P2）

load（写向量库）在 indexing/runner.py：它要和库表、向量库一起做，放这里会让纯计算层碰 IO。

处理规则（kb_process_rule.rules，同 Dify）：
    {
      "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": true},
                               {"id": "remove_urls_emails", "enabled": false}],
      "segmentation": {"separator": "\\n\\n", "max_tokens": 500, "chunk_overlap": 50},
      "parent_mode": "paragraph" | "full_doc",                 # 仅 hierarchical
      "subchunk_segmentation": {"separator": "\\n", "max_tokens": 200, "chunk_overlap": 0}
    }
★ 字段名 max_tokens 沿用 Dify，但按字符计（见 splitter.py）。
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from ..errors import Invalid
from .cleaner import clean
from .models import Document, text_hash
from .splitter import FixedRecursiveCharacterTextSplitter

__all__ = [
    "AUTOMATIC_RULES",
    "HIERARCHICAL_DEFAULT_RULES",
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
    "parent_mode": "paragraph",
    "subchunk_segmentation": {"separator": "\n", "max_tokens": 512, "chunk_overlap": 0},
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
        if parent_mode not in ("paragraph", "full_doc"):
            raise Invalid("parent_mode 只能是 paragraph 或 full_doc")
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
        return mode, out
    if doc_form == "text_model":
        if mode == "automatic":
            return mode, copy.deepcopy(AUTOMATIC_RULES)
        if mode != "custom":
            raise Invalid("通用分段只能用 automatic 或 custom 规则")
        rules = rules or {}
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


def transform(
    documents: list[Document],
    *,
    doc_form: str,
    rules: dict[str, Any],
    preview_limit: int | None = None,
) -> list[Document]:
    """解析结果 → 分段（父子模式带 children）。preview_limit：预览时最多出几段。"""
    if doc_form == "text_model":
        splitter = _splitter(rules["segmentation"])
        out: list[Document] = []
        for doc in documents:
            out.extend(_nodes(clean(doc.page_content, rules), splitter, doc.metadata))
            if preview_limit and len(out) >= preview_limit:
                return out[:preview_limit]
        return out

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
