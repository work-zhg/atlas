"""分段器（参考 Dify core/rag/splitter/，其基类源自 LangChain 的 RecursiveCharacterTextSplitter）。

FixedRecursiveCharacterTextSplitter：
  1. 先按用户分隔符切（自动模式为 "\\n"）
  2. 超过 chunk_size 的块按 ["\\n\\n", "\\n", "。", ". ", " ", ""] 依次递归细分
  3. 相邻小块合并到 chunk_size 以内，按 chunk_overlap 重叠

★ 长度按**字符**计 —— 与 Dify 当前实现一致（EnhanceRecursiveCharacterTextSplitter 的
  length_function 就是 len），不依赖向量模型的 tokenizer。
★ 与 Dify 的一处差异：递归时分隔符按字面量匹配（re.escape）。Dify 的 _split_text 直接把
  ". " 当正则用，会匹配「任意字符 + 空格」。
"""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Callable

__all__ = ["DEFAULT_SEPARATORS", "FixedRecursiveCharacterTextSplitter", "unescape_separator"]

logger = logging.getLogger(__name__)

DEFAULT_SEPARATORS = ["\n\n", "\n", "。", ". ", " ", ""]


def unescape_separator(sep: str) -> str:
    """表单里输入的是转义形式（如 "\\n"），这里还原。

    ★ Dify 直接 codecs.decode(sep, "unicode_escape")，非 ASCII 分隔符（如中文逗号「，」）会被
      当 latin-1 解成乱码，按它切不到任何东西。这里只对 ASCII 部分做转义还原。
    """
    if sep.isascii():
        return codecs.decode(sep, "unicode_escape")
    return re.sub(r"\\[nrt\\]", lambda m: codecs.decode(m.group(0), "unicode_escape"), sep)


def _lengths(texts: list[str]) -> list[int]:
    return [len(t) for t in texts]


class FixedRecursiveCharacterTextSplitter:
    def __init__(
        self,
        *,
        chunk_size: int,
        chunk_overlap: int = 0,
        fixed_separator: str = "\n\n",
        separators: list[str] | None = None,
        keep_separator: bool = True,
        length_function: Callable[[list[str]], list[int]] = _lengths,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if chunk_overlap > chunk_size:
            raise ValueError(f"重叠长度 {chunk_overlap} 不能大于分段长度 {chunk_size}")
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._fixed_separator = unescape_separator(fixed_separator)
        self._separators = separators or DEFAULT_SEPARATORS
        self._keep_separator = keep_separator
        self._length = length_function

    # ------------------------------------------------------------------ 入口

    def split_text(self, text: str) -> list[str]:
        chunks = text.split(self._fixed_separator) if self._fixed_separator else [text]
        final: list[str] = []
        for chunk, size in zip(chunks, self._length(chunks), strict=True):
            if size > self._chunk_size:
                final.extend(self._recursive_split(chunk))
            else:
                final.append(chunk)
        return final

    # ------------------------------------------------------------------ 递归

    def _recursive_split(self, text: str) -> list[str]:
        """Dify recursive_split_text：选第一个出现在文本里的分隔符；空分隔符逐字切。"""
        final: list[str] = []
        separator = self._separators[-1]
        rest: list[str] = []
        for i, sep in enumerate(self._separators):
            if sep == "":
                separator = sep
                break
            if sep in text:
                separator = sep
                rest = self._separators[i + 1 :]
                break

        if separator:
            splits = re.split(r" +", text) if separator == " " else text.split(separator)
            if self._keep_separator:
                splits = [s + separator for s in splits[:-1]] + splits[-1:]
        else:
            splits = list(text)
        if separator == "\n":
            splits = [s for s in splits if s != ""]
        else:
            splits = [s for s in splits if s not in {"", "\n"}]

        joiner = "" if self._keep_separator else separator
        lens = self._length(splits)
        if separator != "":
            good: list[str] = []
            good_lens: list[int] = []
            for s, size in zip(splits, lens, strict=True):
                if size < self._chunk_size:
                    good.append(s)
                    good_lens.append(size)
                    continue
                if good:
                    final.extend(self._merge(good, joiner, good_lens))
                    good, good_lens = [], []
                if not rest:
                    final.append(s)
                else:
                    final.extend(self._split_with(s, rest))
            if good:
                final.extend(self._merge(good, joiner, good_lens))
            return final

        # 逐字：按长度装箱，最后 chunk_overlap 个字符带进下一块
        current, current_len = "", 0
        overlap, overlap_len = "", 0
        for s, size in zip(splits, lens, strict=True):
            if current_len + size <= self._chunk_size - self._chunk_overlap:
                current += s
                current_len += size
            elif current_len + size <= self._chunk_size:
                current += s
                current_len += size
                overlap += s
                overlap_len += size
            else:
                final.append(current)
                current = overlap + s
                current_len = size + overlap_len
                overlap, overlap_len = "", 0
        if current:
            final.append(current)
        return final

    def _split_with(self, text: str, separators: list[str]) -> list[str]:
        """LangChain RecursiveCharacterTextSplitter._split_text（分隔符按字面量）。"""
        final: list[str] = []
        separator = separators[-1]
        rest: list[str] = []
        for i, sep in enumerate(separators):
            if sep == "":
                separator = sep
                break
            if sep in text:
                separator = sep
                rest = separators[i + 1 :]
                break

        if separator:
            if self._keep_separator:
                parts = re.split(f"({re.escape(separator)})", text)
                splits = [parts[i - 1] + parts[i] for i in range(1, len(parts), 2)]
                if len(parts) % 2:
                    splits += parts[-1:]
            else:
                splits = text.split(separator)
        else:
            splits = list(text)
        splits = [s for s in splits if s not in {"", "\n"}]

        joiner = "" if self._keep_separator else separator
        good: list[str] = []
        good_lens: list[int] = []
        for s, size in zip(splits, self._length(splits), strict=True):
            if size < self._chunk_size:
                good.append(s)
                good_lens.append(size)
                continue
            if good:
                final.extend(self._merge(good, joiner, good_lens))
                good, good_lens = [], []
            if not rest:
                final.append(s)
            else:
                final.extend(self._split_with(s, rest))
        if good:
            final.extend(self._merge(good, joiner, good_lens))
        return final

    def _merge(self, splits: list[str], separator: str, lengths: list[int]) -> list[str]:
        """把小块合并到 chunk_size 以内；切下一块时从头部弹出，直到剩余 ≤ chunk_overlap。"""
        sep_len = self._length([separator])[0]
        docs: list[str] = []
        current: list[str] = []
        total = 0
        for d, size in zip(splits, lengths, strict=True):
            if total + size + (sep_len if current else 0) > self._chunk_size:
                if total > self._chunk_size:
                    logger.debug("分段长度 %s 超过设定的 %s", total, self._chunk_size)
                if current:
                    doc = separator.join(current).strip()
                    if doc:
                        docs.append(doc)
                    while total > self._chunk_overlap or (
                        total + size + (sep_len if current else 0) > self._chunk_size and total > 0
                    ):
                        total -= self._length([current[0]])[0] + (
                            sep_len if len(current) > 1 else 0
                        )
                        current = current[1:]
            current.append(d)
            total += size + (sep_len if len(current) > 1 else 0)
        doc = separator.join(current).strip()
        if doc:
            docs.append(doc)
        return docs
