"""出处匹配（doc/rag-eval-design.html §9.2）：判断检索结果是否命中标准出处。

这是检索评测里唯一需要自研的部分：各平台分段不同、段落 ID 互不相通，RAGAS 的整串编辑距离又会
把「大段里包含了出处」判成不相似。规则：

  1. 两边归一化：NFKC（全角转半角）、小写、去掉空白与标点 —— 只比文字本身
  2. 出处包含在段落里 → 命中（full）
  3. 出处被分段边界切开：相邻两段拼起来包含，或与某段的局部相似度 ≥ 阈值 → 部分命中（partial）

部分命中也算命中（计入指标），但在报告里单列，方便抽查。

按段落标注的检索集（C-MTEB，数据集清单写 evidence_match: passage）：出处就是整段语料、一段
正好入库成一个分段，只认「归一化后相等」—— 短标题常被别的段包含，按包含判会误命中。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

__all__ = ["EvidenceMatch", "MatchMode", "match_evidence", "normalize"]

#: quote = 出处是原文片段（包含 / 拼接 / 模糊）；passage = 出处是整段语料（相等）
MatchMode = Literal["quote", "passage"]

#: 局部相似度阈值（rapidfuzz partial_ratio，0~100）
FUZZY_THRESHOLD = 90
#: 太短的出处不做模糊匹配（容易误命中）
_MIN_FUZZY_LEN = 8

_DROP = re.compile(r"[\s　]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S", "Z", "C"):  # 标点、符号（含表格竖线）、空白、控制字符
            continue
        out.append(ch)
    return _DROP.sub("", "".join(out))


@dataclass(frozen=True)
class EvidenceMatch:
    #: 出处在题目 evidence 列表里的下标
    evidence_index: int
    #: 命中的段落排名（1 起）；未命中为 None
    rank: int | None
    kind: str  # full | partial | miss


def match_evidence(
    quotes: list[str], chunks: list[str], *, mode: MatchMode = "quote"
) -> list[EvidenceMatch]:
    """每条出处 → 最早命中它的段落排名。chunks 按检索排名排列。"""
    norm_chunks = [normalize(c) for c in chunks]
    out: list[EvidenceMatch] = []
    for i, quote in enumerate(quotes):
        q = normalize(quote)
        if not q:
            out.append(EvidenceMatch(i, None, "miss"))
            continue
        if mode == "passage":
            rank = next((r for r, c in enumerate(norm_chunks, 1) if c == q), None)
            out.append(EvidenceMatch(i, rank, "full" if rank else "miss"))
            continue
        found: EvidenceMatch | None = None
        for rank, c in enumerate(norm_chunks, 1):
            if q in c:
                found = EvidenceMatch(i, rank, "full")
                break
        if found is None:
            for rank, c in enumerate(norm_chunks, 1):
                joined = c + (norm_chunks[rank] if rank < len(norm_chunks) else "")
                if q in joined or (
                    len(q) >= _MIN_FUZZY_LEN and fuzz.partial_ratio(q, c) >= FUZZY_THRESHOLD
                ):
                    found = EvidenceMatch(i, rank, "partial")
                    break
        out.append(found or EvidenceMatch(i, None, "miss"))
    return out
