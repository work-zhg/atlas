"""读取统一格式的数据集：manifest.yaml + 文档 + questions.jsonl（doc/rag-eval-design.html §5）。

公开评测集由导入器转成同一格式放在 var/bench/datasets/；仓库自带的样例集在 bench/datasets/。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .schema import Corpus, Document, Evidence, Question

__all__ = ["Dataset", "find_dataset", "load_dataset"]


@dataclass(frozen=True)
class Dataset:
    corpus: Corpus
    questions: tuple[Question, ...]
    root: Path

    @property
    def name(self) -> str:
        return self.corpus.name

    def select(self, split: str) -> list[Question]:
        return [q for q in self.questions if split == "all" or q.split == split]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(root: Path) -> Dataset:
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    docs = []
    for d in manifest["documents"]:
        path = (root / d["path"]).resolve()
        digest = _sha256(path)
        if d.get("sha256") and d["sha256"] != digest:
            raise ValueError(f"文档 {d['doc_id']} 的内容与清单记录的 sha256 不一致")
        docs.append(Document(doc_id=d["doc_id"], path=str(path), sha256=digest))
    doc_ids = {d.doc_id for d in docs}

    questions = []
    for line_no, line in enumerate(
        (root / manifest["questions"]).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        evidence = tuple(
            Evidence(
                doc_id=e["doc_id"],
                quote=e.get("quote"),
                page=e.get("page"),
                passage_id=e.get("passage_id"),
            )
            for e in raw.get("evidence") or []
        )
        unknown = {e.doc_id for e in evidence} - doc_ids
        if unknown:
            raise ValueError(f"第 {line_no} 题的出处引用了语料里没有的文档：{sorted(unknown)}")
        questions.append(
            Question(
                id=str(raw["id"]),
                question=raw["question"],
                split=raw.get("split", "test"),
                lang=raw.get("lang", "zh"),
                task=raw.get("task", "qa"),
                tags=tuple(raw.get("tags") or ()),
                reference=raw.get("reference") or "",
                answers=tuple(raw.get("answers") or ()),
                evidence=evidence,
                unanswerable=bool(raw.get("unanswerable", not evidence)),
                official=raw.get("official") or {},
            )
        )
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("题号重复")
    corpus = Corpus(
        name=manifest["name"], version=str(manifest.get("version", "1")), documents=tuple(docs)
    )
    return Dataset(corpus=corpus, questions=tuple(questions), root=root)


def find_dataset(name: str, dirs: list[Path]) -> Dataset:
    for base in dirs:
        root = base / name
        if (root / "manifest.yaml").exists():
            return load_dataset(root)
    raise FileNotFoundError(f"找不到数据集 {name}（在 {', '.join(map(str, dirs))} 下）")
