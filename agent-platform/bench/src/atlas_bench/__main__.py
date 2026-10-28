"""评测命令行（doc/rag-eval-design.html §13）。

python -m atlas_bench run experiments/demo.yaml [--limit N] [--resume <运行目录>] [--no-score]
python -m atlas_bench score <运行目录>        重新判分（不重跑平台）
python -m atlas_bench report <运行目录>       重新生成报告
python -m atlas_bench datasets               列出可用数据集
python -m atlas_bench import cmteb <子集>     导入 C-MTEB 检索子集（如 EcomRetrieval）
python -m atlas_bench cleanup                删除评测在 Atlas 上建的库（库名前缀 bench:）

智能体评测（doc/agent-eval-design.html）：
python -m atlas_bench agent run experiments/agent-demo.yaml [--limit N] [--resume <目录>] [--gate]
    --gate：硬门槛不过（平台错误、沙箱不可用、过程断言失败）时退出码 2
python -m atlas_bench agent report <运行目录>  重新汇总与生成报告（不重跑）
python -m atlas_bench agent compare <基线目录> <新目录>  按题配对比较；回归或硬门槛不过时退出码 3
python -m atlas_bench agent tasks              列出任务套件
python -m atlas_bench agent cleanup            删除评测建的智能体（slug 前缀 bench-）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import yaml

from .datasets import load_dataset
from .llm import LlmClient
from .platforms.atlas import AtlasPlatform
from .report import write_report
from .runner import run_experiment
from .schema import ExperimentConfig
from .score import score_run
from .settings import BenchSettings, get_settings

logger = logging.getLogger("atlas_bench")


def _llm(settings: BenchSettings, concurrency: int = 4) -> LlmClient:
    if settings.llm_api_key is None:
        raise SystemExit(
            "缺少大模型密钥：设置 ATLAS_BENCH_LLM_API_KEY（或 MEMORY_LLM_API_KEY / LITELLM_KEY）"
        )
    return LlmClient(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        cache_path=settings.cache_path,
        timeout_s=settings.llm_timeout_s,
        concurrency=concurrency,
    )


def _atlas(settings: BenchSettings) -> AtlasPlatform:
    return AtlasPlatform(settings.atlas_kb_url, settings.identity_headers())


async def _run(args: argparse.Namespace, settings: BenchSettings) -> int:
    cfg = ExperimentConfig.model_validate(
        yaml.safe_load(Path(args.experiment).read_text(encoding="utf-8"))
    )
    llm = _llm(settings, cfg.concurrency) if cfg.generation != "none" else None
    atlas = _atlas(settings)
    try:
        run_dir = await run_experiment(
            cfg,
            settings,
            platforms={"atlas": atlas},
            llm=llm,
            run_dir=Path(args.resume) if args.resume else None,
            limit=args.limit,
        )
    finally:
        await atlas.aclose()
    print(f"原始结果：{run_dir}")
    if not args.no_score:
        await score_run(run_dir, settings, llm)
        md, page = write_report(run_dir, settings)
        print(f"报告：{md}\n      {page}")
    return 0


async def _score(args: argparse.Namespace, settings: BenchSettings) -> int:
    run_dir = Path(args.run_dir)
    import json

    cfg = ExperimentConfig.model_validate(
        json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["config"]
    )
    llm = _llm(settings, cfg.concurrency) if cfg.generation != "none" else None
    await score_run(run_dir, settings, llm)
    md, page = write_report(run_dir, settings)
    print(f"报告：{md}\n      {page}")
    return 0


def _agent_platform(settings: BenchSettings):
    from .agent.platforms.atlas import AtlasAgentPlatform

    return AtlasAgentPlatform(settings.atlas_api_url, settings.identity_headers())


async def _agent(args: argparse.Namespace, settings: BenchSettings) -> int:
    from .agent.report import write_agent_report
    from .agent.runner import run_agent_experiment
    from .agent.schema import AgentExperimentConfig
    from .agent.tasks import list_suites

    if args.agent_command == "tasks":
        for name, n, path in list_suites():
            print(f"{name:<20} {n:>4} 个任务  {path}")
        return 0
    if args.agent_command == "report":
        md, page = write_agent_report(Path(args.run_dir))
        print(f"报告：{md}\n      {page}")
        return 0
    if args.agent_command == "compare":
        from .agent.report import compare_runs

        res = compare_runs(Path(args.base_dir), Path(args.new_dir))
        for v, x in res["variants"].items():
            d = x["diff"]
            verdict = (
                "回归" if x["regression"] else ("显著变化" if x["significant"] else "无显著差异")
            )
            print(
                f"{v}：成功率差 {d['mean'] * 100:+.1f} 个百分点"
                f"（{d['low'] * 100:+.1f} ~ {d['high'] * 100:+.1f}，{d['n']} 题）→ {verdict}"
            )
            for line in [*x["hard_gate"], *x["warnings"]]:
                print(f"  ⚠ {line}")
            if x["tasks_dropped"]:
                print("  下降的题：" + "、".join(x["tasks_dropped"]))
        (Path(args.new_dir) / "compare.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 3 if res["regression"] else 0
    platform = _agent_platform(settings)
    try:
        if args.agent_command == "cleanup":
            removed = await platform.cleanup_all()
            print("已删除：" + ("、".join(removed) if removed else "（没有评测建的智能体）"))
            return 0
        cfg = AgentExperimentConfig.model_validate(
            yaml.safe_load(Path(args.experiment).read_text(encoding="utf-8"))
        )
        answerer = None
        if cfg.user_model:
            from .agent.user import SimulatedUserAnswerer

            answerer = SimulatedUserAnswerer(_llm(settings), cfg.user_model)
        run_dir = await run_agent_experiment(
            cfg,
            settings,
            platform,
            run_dir=Path(args.resume) if args.resume else None,
            limit=args.limit,
            answerer=answerer,
        )
    finally:
        await platform.aclose()
    md, page = write_agent_report(run_dir)
    print(f"运行目录：{run_dir}\n报告：{md}\n      {page}")
    if getattr(args, "gate", False):
        from .agent.report import hard_gate_failures

        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        if failures := hard_gate_failures(summary):
            print("硬门槛未通过：\n  " + "\n  ".join(failures))
            return 2
        print("硬门槛通过")
    return 0


async def _cleanup(settings: BenchSettings) -> int:
    atlas = _atlas(settings)
    try:
        removed = await atlas.cleanup_all()
    finally:
        await atlas.aclose()
    print("已删除：" + ("、".join(removed) if removed else "（没有评测建的库）"))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "openai", "httpcore", "instructor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m atlas_bench")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("experiment")
    r.add_argument("--limit", type=int, default=None, help="每个数据集只跑前 N 题")
    r.add_argument("--resume", default=None, help="接着跑某个运行目录")
    r.add_argument("--no-score", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("run_dir")
    rp = sub.add_parser("report")
    rp.add_argument("run_dir")
    sub.add_parser("datasets")
    im = sub.add_parser("import", help="导入公开评测集")
    im.add_argument("source", choices=["cmteb"])
    im.add_argument("subset", help="C-MTEB 检索子集，如 EcomRetrieval、DuRetrieval")
    im.add_argument("--per-file", type=int, default=None, help="每个文件放多少段（默认 1000）")
    sub.add_parser("cleanup")
    ag = sub.add_parser("agent", help="智能体工程能力评测")
    agsub = ag.add_subparsers(dest="agent_command", required=True)
    ar = agsub.add_parser("run")
    ar.add_argument("experiment")
    ar.add_argument("--limit", type=int, default=None, help="只跑前 N 个任务")
    ar.add_argument("--resume", default=None, help="接着跑某个运行目录")
    ar.add_argument("--gate", action="store_true", help="硬门槛不过时退出码 2（部署后冒烟用）")
    arp = agsub.add_parser("report")
    arp.add_argument("run_dir")
    acp = agsub.add_parser("compare")
    acp.add_argument("base_dir")
    acp.add_argument("new_dir")
    agsub.add_parser("tasks")
    agsub.add_parser("cleanup")
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "run":
        return asyncio.run(_run(args, settings))
    if args.command == "score":
        return asyncio.run(_score(args, settings))
    if args.command == "report":
        md, page = write_report(Path(args.run_dir), settings)
        print(f"报告：{md}\n      {page}")
        return 0
    if args.command == "datasets":
        for base in settings.datasets_dirs:
            for m in sorted(base.glob("*/manifest.yaml")) if base.exists() else []:
                ds = load_dataset(m.parent)
                splits = {s: len(ds.select(s)) for s in ("dev", "test")}
                docs, total = len(ds.corpus.documents), len(ds.questions)
                print(f"{ds.name:<20} {docs:>5} 篇文档  {total:>5} 题  {splits}  {m.parent}")
        return 0
    if args.command == "import":
        from .importers.cmteb import PER_FILE, import_cmteb

        root, stats = import_cmteb(
            args.subset, settings.datasets_dirs[-1], per_file=args.per_file or PER_FILE
        )
        print(f"已导入：{root}\n  {json.dumps(stats, ensure_ascii=False)}")
        return 0
    if args.command == "cleanup":
        return asyncio.run(_cleanup(settings))
    if args.command == "agent":
        return asyncio.run(_agent(args, settings))
    return 1


if __name__ == "__main__":
    sys.exit(main())
