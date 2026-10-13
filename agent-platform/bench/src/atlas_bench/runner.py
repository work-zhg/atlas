"""运行器（doc/rag-eval-design.html §8）：导入 → 检索 → 生成，原始结果逐题落盘。

    var/bench/runs/<run_id>/
      manifest.json                    实验配置、git commit、数据集指纹、平台信息、库 id
      raw/<数据集>/<平台>__<配置>.jsonl  每题一行：检索结果 + 生成结果（断点续跑按题号跳过）

★ 跑题与判分分离：这里不打分。判分（scoring/）读原始结果，可以离线反复重跑。
★ 同一（语料, 入库配置）在平台上只入库一次；入库配置相同、只是检索方式不同的配置共用一个库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .datasets import find_dataset
from .generate import generate_answer
from .llm import LlmClient
from .platforms.base import KbHandle, Platform
from .schema import ExperimentConfig, Question
from .settings import REPO_ROOT, BenchSettings

__all__ = ["raw_path", "run_experiment"]

logger = logging.getLogger(__name__)


def raw_path(run_dir: Path, dataset: str, platform: str, variant: str) -> Path:
    return run_dir / "raw" / dataset / f"{platform}__{variant}.jsonl"


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not row.get("error"):
                out.add(row["qid"])
    return out


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


async def _one(
    q: Question,
    *,
    platform: Platform,
    kb: KbHandle,
    cfg: ExperimentConfig,
    retrieval: dict[str, Any],
    llm: LlmClient | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"qid": q.id}
    try:
        started = time.monotonic()
        chunks = await platform.retrieve(kb, q.question, top_k=cfg.top_k, retrieval=retrieval)
        row["retrieval"] = {
            "latency_ms": int((time.monotonic() - started) * 1000),
            "chunks": [asdict(c) for c in chunks],
        }
        if cfg.generation == "unified" and llm is not None:
            gens = []
            for r in range(cfg.repeats):
                ans = await generate_answer(llm, cfg.generator_model, q.question, chunks)
                gens.append({"repeat": r, **asdict(ans)})
            row["generations"] = gens
    except Exception as exc:  # 单题失败不中断整批；重跑时会重试这一题
        logger.warning("题目 %s 失败：%s", q.id, exc)
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


async def run_experiment(
    cfg: ExperimentConfig,
    settings: BenchSettings,
    *,
    platforms: dict[str, Platform],
    llm: LlmClient | None,
    run_dir: Path | None = None,
    limit: int | None = None,
    ingest_timeout_s: float = 3600,
) -> Path:
    if run_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_dir = settings.runs_dir / f"{stamp}-{cfg.name.replace(' ', '_')[:40]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    manifest: dict[str, Any] = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {
            "config": cfg.model_dump(),
            "git_commit": _git_commit(),
            "started_at": datetime.now(UTC).isoformat(),
            "llm_base_url": llm.base_url if llm else None,
            "datasets": {},
            "platforms": {},
            "kbs": {},
            "limit": limit,
        }
    )

    def save() -> None:
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    sem = asyncio.Semaphore(cfg.concurrency)

    for ds_name in cfg.datasets:
        ds = find_dataset(ds_name, settings.datasets_dirs)
        questions = ds.select(cfg.split)[: limit or None]
        manifest["datasets"][ds_name] = {
            "version": ds.corpus.version,
            "content_hash": ds.corpus.content_hash,
            "documents": len(ds.corpus.documents),
            "questions": [q.id for q in questions],
        }
        save()
        for pcfg in cfg.platforms:
            platform = platforms[pcfg.adapter]
            manifest["platforms"][pcfg.adapter] = await platform.describe()
            kbs: dict[str, KbHandle] = {}  # 入库配置指纹 → 库
            for vname, variant in pcfg.variants.items():
                ingest_key = json.dumps(variant.ingest, sort_keys=True, ensure_ascii=False)
                if ingest_key not in kbs:
                    kb = await platform.ingest(ds.corpus, variant.ingest)
                    report = await platform.wait_ready(kb, timeout_s=ingest_timeout_s)
                    if report.failed:
                        raise RuntimeError(f"{pcfg.adapter} 有文档入库失败：{report.failed[:3]}")
                    logger.info(
                        "%s/%s 入库就绪：%s（%s 份文档，%s，%.1fs）",
                        pcfg.adapter,
                        vname,
                        kb.name,
                        report.documents,
                        "复用" if kb.reused else "新建",
                        report.seconds,
                    )
                    kbs[ingest_key] = kb
                    manifest["kbs"][f"{ds_name}/{pcfg.adapter}/{vname}"] = {
                        "kb_id": kb.kb_id,
                        "name": kb.name,
                        "reused": kb.reused,
                        "ingest_seconds": report.seconds,
                    }
                    save()
                kb = kbs[ingest_key]
                path = raw_path(run_dir, ds_name, pcfg.adapter, vname)
                path.parent.mkdir(parents=True, exist_ok=True)
                done = _done_ids(path)
                todo = [q for q in questions if q.id not in done]
                logger.info(
                    "%s/%s/%s：%s 题待跑（已完成 %s）",
                    ds_name,
                    pcfg.adapter,
                    vname,
                    len(todo),
                    len(done),
                )

                async def task(
                    q: Question,
                    kb: KbHandle = kb,
                    retrieval: dict[str, Any] = variant.retrieval,
                    platform: Platform = platform,
                ) -> dict[str, Any]:
                    async with sem:
                        return await _one(
                            q, platform=platform, kb=kb, cfg=cfg, retrieval=retrieval, llm=llm
                        )

                with path.open("a", encoding="utf-8") as f:
                    for coro in asyncio.as_completed([task(q) for q in todo]):
                        row = await coro
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        f.flush()

    manifest["finished_at"] = datetime.now(UTC).isoformat()
    save()
    return run_dir
