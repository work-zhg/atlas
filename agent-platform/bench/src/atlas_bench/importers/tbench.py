"""Terminal-Bench 2 导入器：Harbor 格式的任务 → bench 的智能体任务（sandbox 模式原地校验）。

    <任务>/task.toml          元数据、超时、环境（预构建镜像 docker_image、资源）
    <任务>/instruction.md     Agent 看到的指令
    <任务>/environment/       只取 Dockerfile 里的 WORKDIR（镜像已经预构建好，不在这里构建）
    <任务>/tests/             校验：test.sh 把得分写到 /logs/verifier/reward.txt
    <任务>/solution/          参考解（solve.sh）

python -m atlas_bench import tbench [任务…] [--ref 提交]  →  var/bench/agent-tasks/tbench/<任务>/
    另写 SOURCE.yaml（来源）、images.txt（预拉清单）

转换：
    docker_image          → agent_patch.sandbox = {image, profile: bench}（每题一个智能体，可复用）
    tests/                → verifier/（Agent 结束后才解到沙箱的 /tests，同 Harbor）
    solution/             → solution/
    agent.timeout_sec     → budget.wall_s；verifier.timeout_sec → verifier.timeout_s
    WORKDIR               → verifier.cwd，并在指令前写明起始目录

★ 与官方榜单的差异（看结果时要记得）：
  · 资源上限用平台的沙箱配置（SANDBOX_CPU_* / SANDBOX_MEMORY_*），不是 task.toml 的 cpus / memory_mb
  · Atlas 的命令从 /workspace 开始（Harbor 的 shell 在镜像的 WORKDIR）：指令前加了一行起始目录
  · 时间预算从发出第一条消息算起，含沙箱启动；镜像没预拉时首次要现拉，先预拉再跑
  · 校验时现场联网（apt、从 GitHub 装 uv 与 Python）：网络不通会记成不通过 —— 连不上 GitHub 的部署
    要给评测档沙箱配代理（SANDBOX_BENCH_PROXY）
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml

__all__ = ["DEFAULT_REF", "FIRST_BATCH", "REPO", "convert_task", "import_tbench"]

REPO = "harbor-framework/terminal-bench-2"
#: 固定到一个提交：题目会修订，不固定就没法和之前的结果比
DEFAULT_REF = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
SUITE = "tbench"
#: 第一批：镜像小（30–160 MB）、不要 GPU / QEMU、没有加密附件、纯文本可做；
#: 覆盖 easy 3 / medium 10 / hard 2，系统管理类（root 才做得了）有 3 题
FIRST_BATCH = (
    "regex-log",
    "fix-git",
    "cobol-modernization",
    "overfull-hbox",
    "openssl-selfsigned-cert",
    "nginx-request-logging",
    "sqlite-with-gcov",
    "log-summary-date-ranges",
    "kv-store-grpc",
    "sqlite-db-truncate",
    "cancel-async-tasks",
    "constraints-scheduling",
    "git-leak-recovery",
    "vulnerable-secret",
    "configure-git-webserver",
)
#: 只取这些：environment/ 下除 Dockerfile 外的文件是构建镜像用的，镜像已经预构建
_WANTED = re.compile(r"^(task\.toml|instruction\.md|environment/Dockerfile|tests/.+|solution/.+)$")
_WORKDIR = re.compile(r"(?im)^\s*WORKDIR\s+(\S+)")
#: Terminal-Bench 的分类 → bench 的 category（报告按它分组；原分类留在 tags）
_CATEGORY = {
    "security": "security",
    "data-processing": "data",
    "data-science": "data",
    "data-querying": "data",
    "software-engineering": "code",
    "debugging": "code",
}


def convert_task(src: Path, dst: Path) -> dict[str, Any]:
    """一个已下载的 Harbor 任务目录 → bench 任务目录。返回写进 task.yaml 的内容。"""
    meta = tomllib.loads((src / "task.toml").read_text(encoding="utf-8"))
    env, info = meta["environment"], meta.get("metadata") or {}
    dockerfile = src / "environment" / "Dockerfile"
    found = _WORKDIR.findall(dockerfile.read_text(encoding="utf-8")) if dockerfile.exists() else []
    workdir = found[-1] if found else "/"
    instruction = (src / "instruction.md").read_text(encoding="utf-8").strip()
    category = str(info.get("category") or "")
    task = {
        "category": _CATEGORY.get(category, "cli"),
        "tags": ["tbench", category, str(info.get("difficulty") or "")],
        # Harbor 的 shell 起在镜像的 WORKDIR；Atlas 的命令从 /workspace 开始，写明了才公平
        "instruction": f"Working directory: {workdir}\n\n{instruction}",
        "verifier": {
            "mode": "sandbox",
            "command": "bash /tests/test.sh",
            "timeout_s": float(meta["verifier"]["timeout_sec"]),
            "dest": "/tests",
            "cwd": workdir,
            "reward_file": "/logs/verifier/reward.txt",
        },
        "budget": {"wall_s": float(meta["agent"]["timeout_sec"])},
        "agent_patch": {"sandbox": {"image": env["docker_image"], "profile": "bench"}},
    }
    dst.mkdir(parents=True, exist_ok=True)
    for name, target in (("tests", "verifier"), ("solution", "solution")):
        for f in sorted((src / name).rglob("*")) if (src / name).is_dir() else []:
            if f.is_file():
                out = dst / target / f.relative_to(src / name)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(f.read_bytes())
    (dst / "task.yaml").write_text(
        yaml.safe_dump(task, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    return task


def _fetch(client: httpx.Client, ref: str, names: list[str], into: Path) -> None:
    """GitHub：一次取整棵树的清单，再逐个下载需要的文件（整库打包有几十 MB，大多用不到）。"""
    tree = client.get(f"https://api.github.com/repos/{REPO}/git/trees/{ref}?recursive=1")
    tree.raise_for_status()
    # 仓库根下一个目录一个任务：任务名 → 任务内的相对路径
    by_task: dict[str, list[str]] = {}
    for e in tree.json()["tree"]:
        parts = PurePosixPath(e["path"]).parts
        if e["type"] == "blob" and len(parts) > 1:
            by_task.setdefault(parts[0], []).append("/".join(parts[1:]))
    for name in names:
        wanted = [rel for rel in by_task.get(name, []) if _WANTED.match(rel)]
        if "task.toml" not in wanted:
            raise SystemExit(f"{REPO}@{ref[:12]} 里没有任务 {name}")
        for rel in wanted:
            r = client.get(f"https://raw.githubusercontent.com/{REPO}/{ref}/{name}/{rel}")
            r.raise_for_status()
            out = into / name / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)


def import_tbench(
    names: list[str], tasks_root: Path, *, ref: str = DEFAULT_REF, work: Path | None = None
) -> tuple[Path, list[str]]:
    """下载并转换 → (套件目录, 导入的任务)。走环境变量里的代理（HTTPS_PROXY）。"""
    names = names or list(FIRST_BATCH)
    work = work or tasks_root.parent / "cache" / "tbench" / ref
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        missing = [n for n in names if not (work / n / "task.toml").exists()]
        if missing:
            _fetch(client, ref, missing, work)
    suite = tasks_root / SUITE
    for name in names:
        convert_task(work / name, suite / name)
    source = {
        "repo": f"https://github.com/{REPO}",
        "ref": ref,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tasks": sorted({*_existing(suite), *names}),
    }
    (suite / "SOURCE.yaml").write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # 预拉清单：deploy/server/images.sh pull $(cat images.txt)。
    # 镜像都在 Docker Hub：写成全名，与 containerd 列出来的一致（已有就跳过按全名比）
    images = sorted({f"docker.io/{_image(suite / name)}" for name in source["tasks"]})
    (suite / "images.txt").write_text("\n".join(images) + "\n", encoding="utf-8")
    return suite, names


def _image(task_dir: Path) -> str:
    raw = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    return str(raw["agent_patch"]["sandbox"]["image"])


def _existing(suite: Path) -> list[str]:
    return [p.parent.name for p in suite.glob("*/task.yaml")]
