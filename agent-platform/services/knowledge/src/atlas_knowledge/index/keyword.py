"""中文分词、关键词与 BM25 稀疏向量。

    tokenize()        jieba 搜索引擎模式分词，去停用词与标点（BM25 的词表）
    extract_keywords() jieba TF-IDF 关键词（加权重排用，同 Dify JiebaKeywordTableHandler）
    bm25_document() / bm25_query()  稀疏向量，写进 Qdrant 的 bm25 向量

★ 为什么不用 fastembed 的 Qdrant/bm25：它的 SimpleTokenizer 按非单词字符切，中文整句会变成
  一个 token。这里做的事与它相同 —— 文档侧给每个词 BM25 的 TF 部分，查询侧每个词权重 1，
  IDF 交给 Qdrant 的 IDF modifier 在检索时按全集合统计 —— 只是分词换成 jieba。
★ 词 id 用 blake2b 取 4 字节（Qdrant 稀疏向量下标是 uint32）。碰撞概率在知识库规模下可忽略，
  且碰撞只会让两个词共享权重，不会出错。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import Counter

__all__ = ["bm25_document", "bm25_query", "extract_keywords", "tokenize"]

logger = logging.getLogger(__name__)

#: BM25 参数（同 fastembed Bm25 的默认）
_K1 = 1.2
_B = 0.75
_AVG_LEN = 256.0

_STOPWORDS_TEXT = """
的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着
没有 看 好 自己 这 那 之 与 及 其 或 等 被 把 让 向 从 对 而 但 并 如
若 于 为 以 中 个 们 这个 那个 什么 怎么 如何 吗 呢 吧 啊 哦 呀
a an the and or but if of to in on at by for with from as is are was were be been being it this that
these those i you he she we they what which who whom how why when where do does did not no can will
"""
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())

_WORD = re.compile(r"\w", re.UNICODE)
_init_lock = threading.Lock()
_ready = False


def _jieba():  # type: ignore[no-untyped-def]
    """首次使用时初始化 jieba（加载词典约 1 秒），并关掉它的日志。"""
    global _ready
    import jieba

    if not _ready:
        with _init_lock:
            if not _ready:
                jieba.setLogLevel(logging.WARNING)
                jieba.initialize()
                _ready = True
    return jieba


def tokenize(text: str) -> list[str]:
    jieba = _jieba()
    out: list[str] = []
    for tok in jieba.lcut_for_search(text.lower()):
        tok = tok.strip()
        if not tok or tok in STOPWORDS or not _WORD.search(tok):
            continue
        out.append(tok)
    return out


def extract_keywords(text: str, top_k: int | None = 10) -> list[str]:
    _jieba()
    import jieba.analyse

    words = jieba.analyse.extract_tags(text, topK=top_k or 20)
    return [w for w in words if w.lower() not in STOPWORDS]


def _token_id(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "little")


def bm25_document(text: str) -> tuple[list[int], list[float]]:
    tokens = tokenize(text)
    if not tokens:
        return [], []
    counts: Counter[int] = Counter(_token_id(t) for t in tokens)
    doc_len = len(tokens)
    norm = _K1 * (1 - _B + _B * doc_len / _AVG_LEN)
    indices = list(counts)
    values = [counts[i] * (_K1 + 1) / (counts[i] + norm) for i in indices]
    return indices, values


def bm25_query(text: str) -> tuple[list[int], list[float]]:
    ids = list(dict.fromkeys(_token_id(t) for t in tokenize(text)))
    return ids, [1.0] * len(ids)
