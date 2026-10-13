"""评测的数据结构（doc/rag-eval-design.html §5）。

数据集 = 语料（documents）+ 题目（questions）；题目的标准出处按「文档 + 原文片段」等粒度标注，
不依赖任何平台的分段 ID —— 各平台分段不同，只有原文能统一判断是否命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Answer",
    "Chunk",
    "Corpus",
    "Document",
    "Evidence",
    "ExperimentConfig",
    "PlatformConfig",
    "Question",
    "VariantConfig",
]


# ───────────────────────────────────────────── 数据集


@dataclass(frozen=True)
class Document:
    doc_id: str
    path: str  # 本地文件绝对路径
    sha256: str


@dataclass(frozen=True)
class Corpus:
    name: str
    version: str
    documents: tuple[Document, ...]

    @property
    def content_hash(self) -> str:
        """语料内容指纹：同一份语料在平台上只入库一次。"""
        import hashlib

        h = hashlib.sha256()
        for d in sorted(self.documents, key=lambda x: x.doc_id):
            h.update(f"{d.doc_id}\0{d.sha256}\n".encode())
        return h.hexdigest()


@dataclass(frozen=True)
class Evidence:
    doc_id: str
    #: 原文片段（首选）；页 / 段落 ID 粒度留给公开评测集的导入器
    quote: str | None = None
    page: int | None = None
    passage_id: str | None = None


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    split: str = "test"
    lang: str = "zh"
    task: str = "qa"
    tags: tuple[str, ...] = ()
    reference: str = ""
    answers: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    unanswerable: bool = False
    official: dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────── 运行结果


@dataclass
class Chunk:
    """平台返回的一段检索结果（按排名）。text = 交给模型的正文（父子分段时是父块）。"""

    text: str
    score: float | None = None
    doc_name: str | None = None
    title_path: list[str] = field(default_factory=list)
    child_texts: list[str] = field(default_factory=list)


@dataclass
class Answer:
    text: str
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    citations: list[str] = field(default_factory=list)


# ───────────────────────────────────────────── 实验配置


class VariantConfig(BaseModel):
    """同一平台的一种配置。

    ingest 决定入库方式（同 ingest 的配置共用一个库），retrieval 决定检索方式。
    """

    ingest: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)


class PlatformConfig(BaseModel):
    adapter: Literal["atlas"]
    variants: dict[str, VariantConfig]


class ExperimentConfig(BaseModel):
    name: str
    datasets: list[str]
    split: Literal["dev", "test", "all"] = "test"
    top_k: int = 5
    #: 生成重复次数（检索是确定的，只跑一次）
    repeats: int = 1
    concurrency: int = 4
    #: unified = 取平台检索结果，用同一提示词与模型生成；none = 只评检索
    generation: Literal["unified", "none"] = "unified"
    generator_model: str = "deepseek-flash"
    judge_model: str = "deepseek-v4-pro"
    retrieval_metrics: list[str] = Field(
        default_factory=lambda: ["hit", "recall", "mrr", "ndcg", "context_chars"]
    )
    generation_metrics: list[str] = Field(
        default_factory=lambda: [
            "crag",
            "refusal",
            "ragas_faithfulness",
            "ragas_factual_correctness",
        ]
    )
    #: RAGAS 事实正确性的模式。★ 默认 recall（参考答案的要点回答有没有覆盖到）：
    #:   f1 / precision 还要求回答里的每条陈述都能在参考答案中找到，参考答案简短时会把
    #:   正确但更详细的回答判成低分
    factual_correctness_mode: Literal["recall", "precision", "f1"] = "recall"
    platforms: list[PlatformConfig]
