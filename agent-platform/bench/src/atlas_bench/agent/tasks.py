"""任务加载：bench/agent-tasks/<套件>/<任务>/（doc/agent-eval-design.html §14.2）。

    <任务>/task.yaml     instruction、category、tags、verifier、budget
    <任务>/files/        初始工作区（原样上传）
    <任务>/verifier/     校验脚本（Agent 看不到，校验时挂到 /verifier）

公开任务集由导入器转成同一格式后放在 var/bench/agent-tasks/。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..settings import BENCH_DIR, REPO_ROOT
from .schema import AgentExperimentConfig, Task

__all__ = ["TASK_DIRS", "list_suites", "load_suite", "select_tasks"]

TASK_DIRS = [BENCH_DIR / "agent-tasks", REPO_ROOT / "var" / "bench" / "agent-tasks"]
#: 初始文件的单个大小上限（上传接口的上限是 10 MB；任务文件不该大）
_FILE_MAX = 2 * 1024 * 1024


def _suite_dir(name: str) -> Path:
    for base in TASK_DIRS:
        if (base / name).is_dir():
            return base / name
    raise FileNotFoundError(f"找不到任务套件 {name}（在 {', '.join(map(str, TASK_DIRS))} 下）")


def load_suite(name: str) -> list[Task]:
    tasks: list[Task] = []
    for spec in sorted(_suite_dir(name).glob("*/task.yaml")):
        tdir = spec.parent
        raw = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
        files: dict[str, str] = {}
        fdir = tdir / "files"
        if fdir.is_dir():
            for f in sorted(p for p in fdir.rglob("*") if p.is_file()):
                if f.stat().st_size > _FILE_MAX:
                    raise ValueError(f"{f} 超过 {_FILE_MAX} 字节")
                files[f.relative_to(fdir).as_posix()] = f.read_text(encoding="utf-8")
        tasks.append(Task(id=f"{name}/{tdir.name}", suite=name, files=files, dir=str(tdir), **raw))
    return tasks


def list_suites() -> list[tuple[str, int, Path]]:
    out = []
    for base in TASK_DIRS:
        if base.exists():
            for d in sorted(p for p in base.iterdir() if p.is_dir()):
                out.append((d.name, len(list(d.glob("*/task.yaml"))), d))
    return out


def select_tasks(cfg: AgentExperimentConfig, limit: int | None = None) -> list[Task]:
    tasks = [t for s in cfg.suites for t in load_suite(s)]
    if cfg.tags_any:
        tasks = [t for t in tasks if set(t.tags) & set(cfg.tags_any)]
    n = limit if limit is not None else cfg.limit
    return tasks[:n] if n else tasks
