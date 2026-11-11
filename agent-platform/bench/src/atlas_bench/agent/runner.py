"""运行器（doc/agent-eval-design.html §14、§16）：variant × task × trial 矩阵。

    var/bench/runs/<时间>-<实验名>/
      manifest.json                 实验配置、git commit、任务清单、各 variant 的智能体与配置版本
      trials/<variant>.jsonl        每个 trial 一行 TrialResult（断点续跑按 (task, trial) 跳过）
      raw/<variant>/<task>#<n>.json 原始轨迹（事件 + 各 run 用量），离线重判与人工复核用

★ 矩阵打乱顺序执行：避免时段因素（网关拥堵、集群负载）系统性偏向某个 variant。
★ 平台错误（infra_error）单独记，不算 Agent 失败，也不悄悄重试掉 —— 报告首页单列。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..runner import _git_commit
from ..settings import BenchSettings
from .cluster import ClusterError, ClusterOps
from .marks import mark_report
from .platforms.atlas import Answerer, AtlasAgentPlatform, AtlasError, TrialRun
from .schema import AgentExperimentConfig, Task, TrialResult, VariantConfig
from .tasks import select_tasks
from .trajectory import check_expectations, event_invariants, metrics_from_trace
from .verify import verify_in_sandbox, verify_workspace

__all__ = [
    "PLATFORM_ERROR_KINDS",
    "deep_merge",
    "prepare_agents",
    "run_agent_experiment",
    "trials_path",
]

logger = logging.getLogger(__name__)

#: run 失败时，这些 error_kind 的原因在**基础设施**（网关、Pod、平台内部），不在 Agent：
#: 记 platform_error、按协议重跑一次。其余（超步数、上下文溢出、模型拒答）是 Agent / harness
#: 自身的失败，记 agent_error —— 那正是评测要量的东西，不能重跑掉。
PLATFORM_ERROR_KINDS = frozenset(
    {
        "model_unavailable",
        "model_rate_limited",
        "internal_error",
        "interrupted",
        "pod_unavailable",
        "runtime_crashed",
        # CLI：会话 Pod 被终止、bridge 重启过、连不上 bridge、权限模式设不上
        "runtime_interrupted",
        "bridge_lost",
        "runtime_unreachable",
        "mode_unavailable",
        "dependency_unavailable",
        "capability_unavailable",
        # 前置会话失败 / 记忆没抽取出来（评测协议的一部分，见 platforms/atlas.py _run_setup）
        "setup_failed",
    }
)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """字典递归合并，其余（含列表）整体替换。不改动入参。"""
    out = dict(base)
    for k, v in patch.items():
        out[k] = (
            deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        )
    return out


def _agent_key(variant: str, task: Task) -> str:
    return (
        f"{variant}|{json.dumps(task.agent_patch, sort_keys=True)}" if task.agent_patch else variant
    )


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


def sse_metrics(tr: TrialRun, trace: list[dict[str, Any]]) -> dict[str, int]:
    """断线续传检查（E18）：以归档里本 run 的事件为准，看客户端收到的 id 有没有缺口与重复。

    只比 [首个收到的 id, 最后收到的 id] 区间：之前的是首连前就发生的，之后的是
    评测客户端停止收听后才归档的 —— 都不是续传的问题。流式增量不归档，不参与比较。
    """
    got = tr.sse_ids
    if not got:
        return {"sse_received": 0, "sse_missing": 0, "sse_duplicates": 0, "sse_reconnects": 0}
    lo, hi = min(got), max(got)
    archived = {
        int(e["thread_seq"])
        for e in trace
        if e.get("run_id") == tr.run_id and e.get("thread_seq") is not None
    }
    return {
        "sse_received": len(got),
        "sse_missing": len({s for s in archived if lo <= s <= hi} - set(got)),
        "sse_duplicates": len(got) - len(set(got)),
        "sse_reconnects": tr.sse_reconnects,
    }


def _classify(
    tr: TrialRun, passed: bool, metrics: dict[str, Any], *, injected: bool = False
) -> str:
    """injected：任务注入了故障。这时沙箱丢失、结果未知、甚至一时建不起来（lifecycle server 被缩掉）
    都是用例要的现场，不算平台错误；没注入故障时出现它们才是平台问题。"""
    if metrics.get("simulated_user_errors"):
        # ★ 模拟用户出错时问题按推荐回答了（不让 run 卡住），但这已经不是要测的那次对话 ——
        #   评测侧的问题：不进分母，续跑时重做（不能悄悄算成 Agent 没问到 / 碰巧做对）
        return "infra_error"
    if tr.budget_exceeded:
        return "budget_exceeded"
    if passed:
        return "success"
    if tr.status in ("failed", "interrupted") and (tr.error_kind or "") in PLATFORM_ERROR_KINDS:
        return "platform_error"
    sandbox_errors = sum(
        int(metrics.get(k) or 0)
        for k in ("sandbox_unavailable", "sandbox_lost", "sandbox_result_unknown")
    )
    if sandbox_errors and not injected:  # 沙箱错误被当成了工具错误，Agent 没机会做成
        return "platform_error"
    if tr.status in ("failed", "interrupted"):
        return "agent_error"
    return "failure"


def _user_error(tr: TrialRun) -> str | None:
    errors = [q["error"] for q in tr.questions if q["error"]]
    if not errors:
        return None
    return f"模拟用户出错（问题已按推荐回答，本次不计入、续跑时重做）：{errors[0]}"


async def _trial(
    platform: AtlasAgentPlatform,
    agent: dict[str, Any],
    variant: str,
    task: Task,
    trial: int,
    cfg: AgentExperimentConfig,
    run_dir: Path,
    answerer: Answerer | None = None,
    ops: ClusterOps | None = None,
) -> TrialResult:
    """一个 trial；平台错误按协议重跑一次（新会话），重跑仍失败才记 platform_error。"""
    res = await _attempt(platform, agent, variant, task, trial, cfg, run_dir, answerer, ops)
    if res.outcome == "platform_error":
        logger.info("%s %s#%d 平台错误，按协议重跑一次：%s", variant, task.id, trial, res.error)
        res = await _attempt(platform, agent, variant, task, trial, cfg, run_dir, answerer, ops)
        res.retried = True
    return res


async def _reclaim(
    platform: AtlasAgentPlatform,
    tr: TrialRun,
    ops: ClusterOps,
    hosts: set[str],
    task: Task,
    *,
    cli: bool,
    runs: list[dict[str, Any]],
) -> int:
    """资源应在 task.reclaim_within_s 秒内回收干净 → 到时还剩几个（范围见 Task.reclaim_scope）。

    ★ CLI 的会话 Pod 按人共用（一个 Pod 至多几个会话）：删一个会话只停它的 bridge，Pod 还有
      别的会话时不退役。所以 CLI 看的是「这个会话的 bridge 停了」（Pod 没了也算）。
    """
    assert task.reclaim_within_s is not None
    scope = task.reclaim_scope
    image = (task.agent_patch.get("cli") or {}).get("image")
    if scope == "image" and not image:
        raise ClusterError("reclaim_scope: image 要在 agent_patch 里写 cli.image")
    if scope == "thread":
        await platform.delete_thread(tr)
    if scope == "subagents":  # 子会话：子 run 所在的会话（除了主会话）
        sessions = sorted({str(r.get("thread_id")) for r in runs if r.get("thread_id")})
        sessions = [s for s in sessions if s != tr.thread_id]
    else:
        sessions = [tr.thread_id] if cli else [""]  # "" = 原生沙箱：看 Pod 本身
    left = {(pod, s) for pod in hosts for s in sessions}
    deadline = time.monotonic() + task.reclaim_within_s
    while True:
        if scope == "image":
            left = {(pod, "") for pod in await ops.pods_with_image(str(image))}
        else:
            for pod, session in list(left):
                alive = (
                    await ops.cli_session_alive(pod, session)
                    if session
                    else await ops.sandbox_exists(pod)
                )
                if not alive:
                    left.discard((pod, session))
        if not left or time.monotonic() >= deadline:
            return len(left)
        await asyncio.sleep(5.0)


def _turn_metrics(tr: TrialRun) -> dict[str, Any]:
    """每一轮的用时（turns.<n>.*）与预期之外的轮数。"""
    out: dict[str, Any] = {"turns_unexpected": sum(1 for t in tr.turns if not t["expected"])}
    for t in tr.turns:
        for key in ("wall_s", "first_event_s", "cancel_to_end_s"):
            if t.get(key) is not None:
                out[f"turns.{t['n']}.{key}"] = t[key]
    return out


def _declared_turn_missed(tr: TrialRun, task: Task) -> int | None:
    """任务声明了预期终态（run_outcomes）而没对上的第一轮；都对上（或没声明）→ None。"""
    return next(
        (t["n"] for t in tr.turns if t["n"] in task.run_outcomes and not t["expected"]), None
    )


def _workspace_changes(initial: dict[str, str | bytes], files: dict[str, bytes]) -> int:
    """跑完后的工作区与初始文件比，新增、删除、改动了几个文件（平台自己的 .atlas/ 不算）。"""
    before = {k: v.encode() if isinstance(v, str) else v for k, v in initial.items()}
    paths = {p for p in {*before, *files} if not p.startswith(".atlas/")}
    return sum(1 for p in paths if before.get(p) != files.get(p))


async def _attempt(
    platform: AtlasAgentPlatform,
    agent: dict[str, Any],
    variant: str,
    task: Task,
    trial: int,
    cfg: AgentExperimentConfig,
    run_dir: Path,
    answerer: Answerer | None = None,
    ops: ClusterOps | None = None,
) -> TrialResult:
    t0 = time.monotonic()
    base = {"task_id": task.id, "variant": variant, "trial": trial,
            "known_defect": task.known_defect}  # fmt: skip
    policy = task.approval_policy or cfg.approval_policy
    try:
        tr = await platform.run_trial(
            agent, task, trial=trial, approval_policy=policy, answerer=answerer, ops=ops
        )
    except (AtlasError, OSError) as exc:  # 建会话 / 上传 / 发消息失败：评测侧拿不到现场
        return TrialResult(**base, outcome="infra_error", error=str(exc))
    refs = {"thread_id": tr.thread_id, "run_id": tr.run_id, "agent_slug": agent["slug"]}
    in_sandbox = task.verifier.mode == "sandbox"
    try:
        trace, runs = await platform.collect(tr)
        approvals_left = await platform.pending_approvals(tr)
        # sandbox 模式在现场校验，不用把工作区下载回来
        files, skipped = ({}, []) if in_sandbox else await platform.download_workspace(tr)
    except (AtlasError, OSError) as exc:
        return TrialResult(**base, outcome="infra_error", error=f"取结果失败：{exc}", refs=refs)

    raw = run_dir / "raw" / variant / f"{task.id.replace('/', '__')}#{trial}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        json.dumps(
            {
                "trace": trace,
                "runs": runs,
                "approvals": tr.approvals,
                "questions": tr.questions,
                "actions": tr.actions,
                "turns": tr.turns,
                # 会话流上收到的全部事件（含不归档的增量）：正文一致、序号连续据此判断
                "stream": tr.stream,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    metrics = metrics_from_trace(trace, runs)
    metrics.update(event_invariants(trace, tr.stream, runs))
    metrics.update(_turn_metrics(tr))
    metrics["approval_wait_ms"] = sum(a["latency_ms"] for a in tr.approvals)
    metrics["approvals_rejected"] = sum(1 for a in tr.approvals if a["decision"] == "rejected")
    # CLI 的审批带工具类别：被请示的是命令还是写入（accept_edits 下写入不该问）
    metrics["approvals_exec"] = sum(1 for a in tr.approvals if a.get("kind") == "execute")
    metrics["approvals_edit"] = sum(
        1 for a in tr.approvals if a.get("kind") in ("edit", "delete", "move")
    )
    metrics["approvals_left"] = approvals_left
    # 子智能体：用过几个子会话（persistent 应当只有一个）、子 run 里没有以取消收尾的
    subs = [r for r in runs if r.get("parent_run_id")]
    metrics["sub_threads"] = len({str(r.get("thread_id")) for r in subs})
    metrics["sub_runs_not_cancelled"] = sum(1 for r in subs if r.get("status") != "cancelled")
    if task.title_within_s is not None:
        # 自动生成的标题来源是 generated（模型起的）或 fallback（取用户第一句话）
        metrics["title_pending"] = int(tr.title_source not in ("generated", "fallback"))
    if not in_sandbox:
        metrics["workspace_changed"] = _workspace_changes(task.files, files)
    if tr.questions:
        # 模拟用户出错时退回了按推荐：结果仍有效，但要看得见（报告「需求澄清」一节）
        metrics["simulated_user_errors"] = sum(1 for q in tr.questions if q["error"])
    if skipped:
        metrics["skipped_files"] = skipped
    if task.sse_disconnect_after:
        metrics.update(sse_metrics(tr, trace))
    if task.thread_memory == "on":
        metrics["memories_new"] = tr.memories_new
        # 先清记忆再校验：服务账号的记忆在所有评测之间共享，不能留给下一题
        metrics["memories_removed"] = await platform.cleanup_memories(tr)
    marks = mark_report(files, trace, tr.approvals)
    metrics.update(marks.metrics)
    if task.injects_faults():
        # 注入了故障（或用例自己制造故障）：沙箱错误是预期内的，报告的硬门槛不数它们
        metrics["faults_injected"] = 1
    if task.actions:
        metrics["actions_done"] = sum(1 for a in tr.actions if a["ok"])
        metrics["actions_failed"] = sum(1 for a in tr.actions if not a["ok"])
        metrics["actions_pending"] = tr.actions_pending
    deleted = False
    if task.reclaim_within_s is not None and ops is not None:
        try:
            metrics["sandboxes_left"] = await _reclaim(
                platform, tr, ops, marks.hosts, task, cli=agent.get("kind") == "cli", runs=runs
            )
            deleted = task.reclaim_scope == "thread"
        except ClusterError as exc:
            return TrialResult(**base, outcome="infra_error", error=f"回收检查没做成：{exc}",
                               refs=refs, actions=tr.actions)  # fmt: skip
    expectations, expect_ok = check_expectations(task.expect, trace, metrics)

    # 即使 run 失败 / 超预算也照样校验：工作区里可能已经做对了（只是没好好收尾）
    if in_sandbox:
        v = await verify_in_sandbox(platform, tr, task)
    else:
        v = await verify_workspace(task, files)
    if v.passed and _declared_turn_missed(tr, task):
        # ★ 任务声明的终态也是结果的一部分：没对上就不算做成（「没有产物」的题，校验脚本对初始
        #   状态天然通过 —— 预期取消却因为模型不可用失败了，不能记成成功）
        v.passed, v.score = False, 0.0
        v.output = f"{v.output}\n[第 {_declared_turn_missed(tr, task)} 轮的终态与任务声明的不符]"
    injection_failed = next((a for a in tr.actions if not a["ok"]), None)
    if v.infra_error:
        outcome = "infra_error"
    elif injection_failed is not None:
        # 故障没注入成：这次 trial 测不到要测的东西，算评测侧的问题，续跑时重做
        outcome = "infra_error"
        v.infra_error = f"故障注入 {injection_failed['do']} 没做成：{injection_failed['detail']}"
    else:
        outcome = _classify(tr, v.passed, metrics, injected=task.injects_faults())
    if cfg.delete_threads and not deleted:
        await platform.delete_thread(tr)
    return TrialResult(
        **base,
        outcome=outcome,
        passed=v.passed and not tr.budget_exceeded,
        score=v.score if not tr.budget_exceeded else 0.0,
        verifier_output=v.output,
        error=tr.budget_exceeded or tr.error or v.infra_error or _user_error(tr),
        metrics=metrics,
        final_message=_final_message(trace),
        wall_s=round(time.monotonic() - t0, 1),
        refs=refs,
        expectations=expectations,
        expect_ok=expect_ok,
        actions=tr.actions,
    )


async def prepare_agents(
    cfg: AgentExperimentConfig, platform: AtlasAgentPlatform, tasks: list[Task]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """建好（或找到）被测的智能体 → (各 variant 的, 带 agent_patch 的任务另建的)。

    ★ 带 agent_patch 的任务用「被测配置 + 补丁」另建一个智能体（slug 带配置指纹，可复用）。
      预先建好：并发的 trial 同时去建同一个 slug 会撞唯一约束。
    """
    agents = {v: await platform.prepare(v, vc) for v, vc in cfg.variants.items()}
    patched: dict[str, dict[str, Any]] = {}
    for v, vc in cfg.variants.items():
        for t in tasks:
            if t.agent_patch and (key := _agent_key(v, t)) not in patched:
                merged = VariantConfig(
                    adapter=vc.adapter,
                    agent=deep_merge(vc.agent, t.agent_patch),
                    description=vc.description,
                )
                patched[key] = await platform.prepare(v, merged)
    return agents, patched


async def run_agent_experiment(
    cfg: AgentExperimentConfig,
    settings: BenchSettings,
    platform: AtlasAgentPlatform,
    *,
    run_dir: Path | None = None,
    limit: int | None = None,
    answerer: Answerer | None = None,
    ops: ClusterOps | None = None,
) -> Path:
    """answerer：模拟用户（agent/user.py）。选中的任务里有带 user 的就必须给 —— 否则问题会按推荐项
    回答，需求澄清评测的结果就没有意义了，宁可早报。
    ops：集群操作。任务带故障动作或回收检查时必须给（同理宁可早报）。"""
    tasks = select_tasks(cfg, limit)
    if not tasks:
        raise SystemExit("没有选中任何任务")
    if answerer is None and any(t.user for t in tasks):
        raise SystemExit("有任务需要模拟用户回答提问：在实验配置里设置 user_model")
    if ops is None and any(t.needs_cluster() for t in tasks):
        raise SystemExit("有任务要注入故障或检查资源回收：需要集群操作权限（kubectl）")
    # 上次评测进程被直接杀掉时没来得及恢复的故障，开跑前先清掉
    if (
        ops is not None
        and any(t.injects_faults() for t in tasks)
        and (cleared := await ops.clear_stale_faults())
    ):
        logger.warning("清掉了上次残留的 %d 个故障", cleared)
    platform.retry_window_s = cfg.api_retry_s
    if run_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_dir = settings.runs_dir / f"{stamp}-{cfg.name}"
    run_dir.mkdir(parents=True, exist_ok=True)

    agents, patched = await prepare_agents(cfg, platform, tasks)
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
        "patched_agents": {k: a["slug"] for k, a in patched.items()},
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
    #: serial 任务的 trial 彼此不并发（共享状态，如用户记忆）
    serial_locks = {t.id: asyncio.Lock() for t in tasks if t.serial}
    finished = 0

    async def one(variant: str, task: Task, n: int) -> None:
        nonlocal finished
        agent = patched.get(_agent_key(variant, task)) or agents[variant]
        async with contextlib.AsyncExitStack() as stack:
            if task.id in serial_locks:  # 先排 serial 锁再占并发名额，不白占名额
                await stack.enter_async_context(serial_locks[task.id])
            await stack.enter_async_context(sem)
            res = await _trial(platform, agent, variant, task, n, cfg, run_dir, answerer, ops)
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
