"""入库增强（doc/knowledge-enterprise-design.html §5.3 / §5.5）：上下文补全 L2、大表摘要。

都用 LLM、都按知识库开关（kb_dataset.enhancements），都可以失败：
单个分段的调用失败只是这个分段少了增强，不影响入库。

    contextual      每个父分段生成一句「这一段在全文中讲什么」，拼进向量化前缀
                    （Contextual Retrieval）。
                    文档正文作为固定前缀（同一文档的所有调用前缀相同 → 命中网关的前缀缓存）；
                    超长文档改用分段所在的邻近内容。
                    结果按（模型, 提示词版本, 文档哈希, 分段哈希）缓存在知识库的库里
                    （segment/enhance_cache.py，保留 30 天）
    table_summary   行数达到阈值的表格生成一段摘要（主题、维度、关键数字），作为子块入口
                    （type=summary）：汇总类的问题（「哪种假期最长」）匹配不到任何单行时，由它召回。
                    表格从 DocIR 取（不从分段里找：普通分段可能把一张表切成很多段），
                    摘要挂到包含这张表单元格最多的分段上
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

from ..models.llm import ChatModel
from ..parse.docir import DocIR, table_markdown
from ..ragdoc import Document, text_hash
from .enhance_cache import EnhanceCache

__all__ = [
    "DEFAULT_ENHANCEMENTS",
    "apply_llm_context",
    "enhance",
    "normalize_enhancements",
    "tables_of",
]

logger = logging.getLogger(__name__)

DEFAULT_ENHANCEMENTS: dict[str, Any] = {
    "contextual": False,
    "table_summary": False,
    "table_summary_min_rows": 8,
}

_TABLE = re.compile(r"(^\|.*\|[ \t]*$\n?)+", re.M)
_SEPARATOR = re.compile(r"^[\s|:\-]+$")

_CONTEXT_PROMPT = """下面是一份文档，以及其中的一个片段。
请用一两句话说明这个片段在整份文档中的位置与主题，帮助检索时理解它
（例如它属于哪个制度的哪一部分、讲的是什么对象）。只输出这一两句话，不要复述片段内容。

<document>
{document}
</document>

<chunk>
{chunk}
</chunk>"""

_TABLE_PROMPT = """下面是一张表格（Markdown）及它所在的章节。请写一段 100 字以内的摘要：表格的主题、
有哪些维度（列）、覆盖哪些对象（行），以及最大 / 最小 / 最值得注意的数值。只输出摘要。

章节：{path}

{table}"""

#: 提示词版本 = 模板文字的哈希，写进缓存键：改了提示词，重新入库就不会读到旧提示词的结果
_CONTEXT_VERSION = text_hash(_CONTEXT_PROMPT)[:12]
_TABLE_VERSION = text_hash(_TABLE_PROMPT)[:12]


def normalize_enhancements(raw: Any) -> dict[str, Any]:
    cfg = {**DEFAULT_ENHANCEMENTS, **(raw if isinstance(raw, dict) else {})}
    return {
        "contextual": bool(cfg["contextual"]),
        "table_summary": bool(cfg["table_summary"]),
        "table_summary_min_rows": max(2, int(cfg["table_summary_min_rows"])),
    }


def tables_of(ir: DocIR) -> list[list[list[str]]]:
    """文档里的表格（每张表 = 行列表，第一行是表头）：
    表格块直接取；纯文本类格式找 Markdown 表格。"""
    out: list[list[list[str]]] = []
    for b in ir.blocks:
        if b.type == "table" and b.table and b.table.get("rows"):
            out.append([[str(c or "") for c in r] for r in b.table["rows"]])
        elif b.type == "text":
            for m in _TABLE.finditer(b.text):
                rows = [
                    [c.strip() for c in ln.strip().strip("|").split("|")]
                    for ln in m.group(0).splitlines()
                    if ln.strip() and not _SEPARATOR.match(ln)
                ]
                if rows:
                    out.append(rows)
    return out


def _owner(nodes: list[Document], rows: list[list[str]]) -> Document | None:
    """包含这张表单元格最多的分段（并列取靠前的）。"""
    cells = {c for r in rows[1:] for c in r if len(c) >= 2}
    best, best_score = None, 0
    for node in nodes:
        score = sum(1 for c in cells if c in node.page_content)
        if score > best_score:
            best, best_score = node, score
    return best


def _doc_excerpt(full: str, chunk: str, limit: int) -> str:
    """长文档：取片段所在位置的前后各一半预算；找不到片段就取开头。"""
    if len(full) <= limit:
        return full
    pos = full.find(chunk[:80])
    if pos < 0:
        return full[:limit]
    start = max(0, pos - limit // 2)
    return full[start : start + limit]


async def enhance(
    nodes: list[Document],
    *,
    full_text: str,
    tables: list[list[list[str]]] | None = None,
    cfg: dict[str, Any],
    llm: ChatModel | None,
    cache: EnhanceCache | None = None,
    doc_chars: int = 30_000,
    concurrency: int = 4,
) -> dict[str, int]:
    """就地增强分段：metadata.llm_context（拼进向量化前缀），表格摘要追加为 type=summary 的子块。

    cache：增强结果缓存（重跑入库不用再调模型）；None = 不缓存。
    返回统计（写进解析报告）：生成了多少上下文、多少摘要、失败多少。
    """
    stats = {"contextual": 0, "table_summaries": 0, "failed": 0}
    if llm is None or not (cfg.get("contextual") or cfg.get("table_summary")):
        return stats
    sem = asyncio.Semaphore(max(1, concurrency))
    doc_hash = text_hash(full_text)

    # 要增强的：(缓存键, 给模型的提示词, 最多几个 token, 结果怎么用)
    jobs: list[tuple[str, str, int, Any]] = []
    if cfg.get("contextual"):
        for node in nodes:
            excerpt = _doc_excerpt(full_text, node.page_content, doc_chars)
            key = f"ctx:{llm.model}:{_CONTEXT_VERSION}:{doc_hash}:{text_hash(node.page_content)}"
            prompt = _CONTEXT_PROMPT.format(document=excerpt, chunk=node.page_content[:4000])
            jobs.append((key, prompt, 160, _context_into(node, stats)))
    if cfg.get("table_summary"):
        min_rows = cfg["table_summary_min_rows"]
        for rows in tables or []:
            node = _owner(nodes, rows) if len(rows) - 1 >= min_rows else None
            if node is None:
                continue
            table = table_markdown(rows)
            path = " > ".join(node.metadata.get("title_path") or [])
            key = f"tbl:{llm.model}:{_TABLE_VERSION}:{text_hash(path + table)}"
            prompt = _TABLE_PROMPT.format(path=path or "（无）", table=table[:6000])
            jobs.append((key, prompt, 300, _summary_into(node, path, stats)))
    if not jobs:
        return stats

    # 开始前一次取回全部命中的；没命中的调模型，每次成功就存一条（中途被杀，做完的不丢）
    hits = await cache.get_many(k for k, *_ in jobs) if cache is not None else {}

    async def run(key: str, prompt: str, max_tokens: int, use: Any) -> None:
        value = hits.get(key)
        if value is None:
            try:
                async with sem:
                    raw = await llm.complete(
                        [{"role": "user", "content": prompt}], max_tokens=max_tokens
                    )
                value = raw.strip() or None
            except Exception:
                logger.warning("入库增强的 LLM 调用失败，这一项跳过", exc_info=True)
                value = None
            if value and cache is not None:
                await cache.put(key, value)
        if value:
            use(value)
        else:
            stats["failed"] += 1

    await asyncio.gather(*(run(*job) for job in jobs))
    return stats


def _context_into(node: Document, stats: dict[str, int]) -> Any:
    def use(value: str) -> None:
        node.metadata["llm_context"] = value[:400]
        stats["contextual"] += 1

    return use


def _summary_into(node: Document, path: str, stats: dict[str, int]) -> Any:
    def use(value: str) -> None:
        node.children = [
            *(node.children or []),
            Document(
                page_content=value,
                metadata={
                    "doc_id": str(uuid.uuid4()),
                    "doc_hash": text_hash(value),
                    "context": node.metadata.get("context") or path,
                    "type": "summary",
                },
            ),
        ]
        stats["table_summaries"] += 1

    return use


def apply_llm_context(nodes: list[Document]) -> None:
    """把生成的上下文拼进分段与它所有子块的向量化前缀（metadata.context）。"""
    for node in nodes:
        extra = node.metadata.pop("llm_context", None)
        if not extra:
            continue
        node.metadata["context"] = "\n".join(p for p in (node.metadata.get("context"), extra) if p)
        for child in node.children or []:
            child.metadata["context"] = "\n".join(
                p for p in (child.metadata.get("context"), extra) if p
            )
