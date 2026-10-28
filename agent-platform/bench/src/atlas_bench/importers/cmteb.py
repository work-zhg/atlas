"""C-MTEB 检索子集导入器（doc/rag-eval-review.html §5.1）：语料、查询全量，nDCG@10 可对照榜单。

    C-MTEB/<子集>          corpus / queries 两个划分（id, text）
    C-MTEB/<子集>-qrels    dev 划分（qid, pid, score）

python -m atlas_bench import cmteb EcomRetrieval  →  var/bench/datasets/cmteb-ecom/

★ 打包入库：10 万段一段一个文档，平台要跑 10 万个入库任务、向量按文档分批，比的就成了入库开销。
  改为每个文件放 per_file 段、段与段之间空一行；实验配置用通用分段 + 分隔符 "\\n\\n"，
  一段正好入库成一个分段。为此段内的连续空行压成一个换行。
★ 超过 max_chars 的段截断：平台按分段长度上限（默认 4000 字）会把它再切开，就和标注对不上了。
  bge 系列向量模型只看前 512 个 token，截断不影响稠密检索。
★ 出处 = 整段原文，清单写 evidence_match: passage（按相等判命中，scoring/matching.py）。
  语料里文字相同的段会互相算命中，条数记在清单 stats.duplicate_passages。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..scoring.matching import normalize

__all__ = ["MAX_CHARS", "PER_FILE", "SUBSETS", "dataset_name", "import_cmteb", "write_dataset"]

SUBSETS = (
    "T2Retrieval",
    "MMarcoRetrieval",
    "DuRetrieval",
    "CovidRetrieval",
    "CmedqaRetrieval",
    "EcomRetrieval",
    "MedicalRetrieval",
    "VideoRetrieval",
)
#: 知识库通用分段的长度上限（atlas_knowledge 的 max_segmentation_length）
MAX_CHARS = 4000
PER_FILE = 1000

_BLANK_LINES = re.compile(r"\n\s*\n")


def dataset_name(subset: str) -> str:
    return "cmteb-" + subset.removesuffix("Retrieval").lower()


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _BLANK_LINES.sub("\n", text).strip()


def write_dataset(
    root: Path,
    *,
    name: str,
    version: str,
    source: dict[str, Any],
    passages: Iterable[tuple[str, str]],
    queries: Iterable[tuple[str, str]],
    qrels: Iterable[tuple[str, str, int]],
    per_file: int = PER_FILE,
    max_chars: int = MAX_CHARS,
) -> dict[str, int]:
    """（pid, 原文）、（qid, 查询）、（qid, pid, 分数）→ 统一格式的数据集目录。返回统计。"""
    if root.exists():
        raise FileExistsError(f"{root} 已存在：要重新导入先删掉它")
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True)

    stats: Counter[str] = Counter()
    where: dict[str, tuple[str, str]] = {}  # pid → (文件, 写进文件的原文)
    seen: Counter[str] = Counter()
    documents: list[dict[str, str]] = []
    batch: list[str] = []

    def doc_id() -> str:
        return f"part-{len(documents) + 1:05d}.txt"

    def flush() -> None:
        path = docs_dir / doc_id()
        path.write_text("\n\n".join(batch) + "\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        documents.append({"doc_id": path.name, "path": f"docs/{path.name}", "sha256": digest})
        batch.clear()

    for pid, raw in passages:
        text = _clean(raw)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
            stats["truncated_passages"] += 1
        key = normalize(text)
        if not key:  # 空段或只有标点：平台不会入库，也无从判命中
            stats["empty_passages"] += 1
            continue
        seen[key] += 1
        where[str(pid)] = (doc_id(), text)
        batch.append(text)
        if len(batch) >= per_file:
            flush()
    if batch:
        flush()
    stats["passages"] = len(where)
    stats["files"] = len(documents)
    stats["duplicate_passages"] = sum(n - 1 for n in seen.values())

    relevant: dict[str, list[str]] = defaultdict(list)
    for qid, pid, score in qrels:
        if score > 0:
            relevant[str(qid)].append(str(pid))
    lines = []
    for qid, text in queries:
        qid = str(qid)
        pids = list(dict.fromkeys(relevant.get(qid, ())))
        found = [p for p in pids if p in where]
        stats["qrels_missing_passage"] += len(pids) - len(found)
        if not found or not text.strip():
            stats["queries_skipped"] += 1
            continue
        stats["evidence"] += len(found)
        lines.append(
            json.dumps(
                {
                    "id": qid,
                    "split": "test",
                    "lang": "zh",
                    "task": "retrieval",
                    "question": text.strip(),
                    "evidence": [
                        {"doc_id": where[p][0], "quote": where[p][1], "passage_id": p}
                        for p in found
                    ],
                    "unanswerable": False,
                },
                ensure_ascii=False,
            )
        )
    (root / "questions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    stats["queries"] = len(lines)

    out = {k: int(v) for k, v in sorted(stats.items())}
    manifest = {
        "name": name,
        "version": version,
        "lang": "zh",
        "license": f"见 {source.get('url', source.get('repo', ''))}",
        "source": source,
        "evidence_match": "passage",
        "stats": out,
        "documents": documents,
        "questions": "questions.jsonl",
    }
    (root / "manifest.yaml").write_text(
        "# 由 python -m atlas_bench import cmteb 生成（importers/cmteb.py），不要手改\n"
        + yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return out


def import_cmteb(subset: str, out_root: Path, *, per_file: int = PER_FILE) -> tuple[Path, dict]:
    """从 HuggingFace 下载（按当时的 revision 固定，记进清单）→ 写到 out_root/<数据集名>。"""
    if subset not in SUBSETS:
        raise ValueError(f"没有这个子集：{subset}（可选：{', '.join(SUBSETS)}）")
    try:  # 只有导入时用到；ragas 已经依赖它，不单独声明
        from datasets import load_dataset
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise SystemExit("导入 C-MTEB 需要 datasets 包：uv pip install datasets") from exc

    repo, qrels_repo = f"C-MTEB/{subset}", f"C-MTEB/{subset}-qrels"
    api = HfApi()
    rev, qrels_rev = api.dataset_info(repo).sha, api.dataset_info(qrels_repo).sha
    corpus = load_dataset(repo, split="corpus", revision=rev)
    queries = load_dataset(repo, split="queries", revision=rev)
    qrels = load_dataset(qrels_repo, split="dev", revision=qrels_rev)
    root = out_root / dataset_name(subset)
    stats = write_dataset(
        root,
        name=dataset_name(subset),
        version=(rev or "unknown")[:8],
        source={
            "repo": repo,
            "revision": rev,
            "qrels_repo": qrels_repo,
            "qrels_revision": qrels_rev,
            "url": f"https://huggingface.co/datasets/{repo}",
        },
        passages=zip(corpus["id"], corpus["text"], strict=True),
        queries=zip(queries["id"], queries["text"], strict=True),
        qrels=zip(qrels["qid"], qrels["pid"], qrels["score"], strict=True),
        per_file=per_file,
    )
    return root, stats
