"""运行器（doc/agent-eval-design.html §10）：variant × task × trial 矩阵。

    var/bench/runs/<时间>-<实验名>/
      manifest.json                 实验配置、git commit、任务清单、各 variant 的智能体与配置版本
      trials/<variant>.jsonl        每个 trial 一行 TrialResult（断点续跑按 (task, trial) 跳过）
      raw/<variant>/<task>#<n>.json 原始轨迹（事件 + 各 run 用量），离线重判与人工复核用

★ 矩阵打乱顺序执行：避免时段因素（网关拥堵、集群负载）系统性偏向某个 variant。
★ 平台错误（infra_error）单独记，不算 Agent 失败，也不悄悄重试掉 —— 报告首页单列。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..runner import _git_commit
from ..settings import BenchSettings
from .platforms.atlas import AtlasAgentPlatform, AtlasError
from .schema import AgentExperimentConfig, Task, TrialResult
from .tasks import select_tasks
from .trajectory import metrics_from_trace
from .verify import verify_workspace

__all__ = ["run_agent_experiment", "trials_path"]

logger = logging.getLogger(__name__)


def trials_path(run_dir: Path, variant: str) -> Path:
    return run_dir / "trials" / f"{variant}.jsonl"


def _done(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["outcome"] != "infra_error":  # 平台错误的 trial 续跑时重来
                out.add((r["task_id"], r["trial"]))
    return out


def _final_message(trace: list[dict[str, Any]]) -> str:
    for e in reversed(trace):
        if e.get("type") == "message.completed" and int(e.get("depth") or 0) == 0:
            content = (e.get("data") or {}).get("content")
            if isinstance(content, str):
                return content[-2000:]
            if isinstance(content, list):
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                return text[-2000:]
    return ""


async def _trial(
    platform: AtlasAgentPlatform,
    agent: dict[str, Any],
    variant: str,
    task: Task,
    trial: int,
    cfg: AgentExperimentConfig,
    run_dir: Path,
) -> TrialResult:
    t0 = time.monotonic()
    base = {"task_id": task.id, "variant": variant, "trial": trial}
    try:
        tr = await platform.run_trial(agent, task, trial=trial, approval_policy=cfg.approval_policy)
    except (AtlasError, OSError) as exc:  # 建会话 / 上传 / 发消息失败：平台问题
        return TrialResult(**base, outcome="infra_error", error=str(exc))
    refs = {"thread_id": tr.thread_id, "run_id": tr.run_id, "agent_slug": agent["slug"]}
    try:
        trace, runs = await platform.collect(tr)
        files, skipped = await platform.download_workspace(tr)
    except (AtlasError, OSError) as exc:
        return TrialResult(**base, outcome="infra_error", error=f"取结果失败：{exc}", refs=refs)

    raw = run_dir / "raw" / variant / f"{task.id.replace('/', '__')}#{trial}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        json.dumps({"trace": trace, "runs": runs, "approvals": tr.approvals}, ensure_ascii=False),
        encoding="utf-8",
    )
    metrics = metrics_from_trace(trace, runs)
    metrics["approval_wait_ms"] = sum(a["latency_ms"] for a in tr.approvals)
    if skipped:
        metrics["skipped_files"] = skipped

    # 即使 run 失败 / 超预算也照样校验：工作区里可能已经做对了（只是没好好收尾）
    v = await verify_workspace(task, files)
    if v.infra_error:
        outcome = "infra_error"
    elif tr.budget_exceeded:
        outcome = "budget_exceeded"
    elif v.passed:
        outcome = "success"
    elif tr.status in ("failed", "interrupted"):
        outcome = "agent_error"
    else:
        outcome = "failure"
    if cfg.delete_threads:
        await platform.delete_thread(tr)
    return TrialResult(
        **base,
        outcome=outcome,
        passed=v.passed and not tr.budget_exceeded,
        score=v.score if not tr.budget_exceeded else 0.0,
        verifier_output=v.output,
        error=tr.budget_exceeded or tr.error or v.infra_error,
        metrics=metrics,
        final_message=_final_message(trace),
        wall_s=round(time.monotonic() - t0, 1),
        refs=refs,
    )


async def run_agent_experiment(
    cfg: AgentExperimentConfig,
    settings: BenchSettings,
    platform: AtlasAgentPlatform,
    *,
    run_dir: Path | None = None,
    limit: int | None = None,
) -> Path:
    tasks = select_tasks(cfg, limit)
    if not tasks:
        raise SystemExit("没有选中任何任务")
    if run_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_dir = settings.runs_dir / f"{stamp}-{cfg.name}"
    run_dir.mkdir(parents=True, exist_ok=True)

    agents = {v: await platform.prepare(v, vc) for v, vc in cfg.variants.items()}
    manifest = {
        "kind": "agent",
        "config": cfg.model_dump(mode="json"),
        "git_commit": _git_commit(),
        "started_at": datetime.now(UTC).isoformat(),
        "platform": await platform.describe(),
        "tasks": [{"id": t.id, "category": t.category, "tags": t.tags} for t in tasks],
        "agents": {
            v: {"id": a["id"], "slug": a["slug"], "version": a.get("version")}
            for v, a in agents.items()
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    jobs = []
    for variant in cfg.variants:
        done = _done(trials_path(run_dir, variant))
        for task in tasks:
            for n in range(1, cfg.trials + 1):
                if (task.id, n) not in done:
                    jobs.append((variant, task, n))
    random.Random(20261013).shuffle(jobs)
    logger.info(
        "共 %d 个 trial 待跑（%d 任务 × %d 配置 × %d 次）",
        len(jobs),
        len(tasks),
        len(cfg.variants),
        cfg.trials,
    )

    sem = asyncio.Semaphore(cfg.concurrency)
    lock = asyncio.Lock()
    finished = 0

    async def one(variant: str, task: Task, n: int) -> None:
        nonlocal finished
        async with sem:
            res = await _trial(platform, agents[variant], variant, task, n, cfg, run_dir)
        async with lock:
            path = trials_path(run_dir, variant)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(res.model_dump_json() + "\n")
            finished += 1
            logger.info(
                "[%d/%d] %s %s#%d → %s（%.0fs）%s",
                finished, len(jobs), variant, task.id, n, res.outcome, res.wall_s,
                f"：{res.error[:120]}" if res.error else "",
            )  # fmt: skip

    await asyncio.gather(*(one(*j) for j in jobs))
    return run_dir
