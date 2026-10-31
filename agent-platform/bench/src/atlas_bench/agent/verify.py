"""校验（doc/agent-eval-design.html §8.1）：Agent 结束后跑任务自带的校验脚本。

container（verify_workspace）
  把最终工作区拷进隔离容器跑。
  ★ 不在评测机本地直接跑：工作区里是模型写的代码，校验常常要执行它 —— 一律进容器，且无网络。
  ★ 校验脚本挂到 /verifier（只读），Agent 从头到尾看不到它。
  ★ 结果 = 退出码（0 通过）；脚本可在 stdout 最后一行写 `SCORE=<0~1>` 给部分得分。

sandbox（verify_in_sandbox）
  在 Agent 用过的评测档沙箱里原地跑（Terminal-Bench：校验的是环境状态 —— 装了什么、
  服务起没起、工作区以外的文件）。同 Harbor：Agent 结束后才把校验脚本送进去，结果是得分文件。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import posixpath
import shlex
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .schema import Task

if TYPE_CHECKING:
    from .platforms.atlas import AtlasAgentPlatform, TrialRun

__all__ = ["VerifyResult", "verify_in_sandbox", "verify_workspace"]

_TAIL = 4000


@dataclass
class VerifyResult:
    passed: bool
    score: float
    output: str
    infra_error: str | None = None


def _score(output: str, passed: bool) -> float:
    for line in reversed(output.strip().splitlines()):
        if line.startswith("SCORE="):
            try:
                return max(0.0, min(1.0, float(line.removeprefix("SCORE=").strip())))
            except ValueError:
                break
    return 1.0 if passed else 0.0


async def verify_workspace(task: Task, files: dict[str, bytes]) -> VerifyResult:
    with tempfile.TemporaryDirectory(prefix="atlas-bench-") as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        for rel, data in files.items():
            target = (ws / rel).resolve()
            if not str(target).startswith(str(ws.resolve())):  # 防止 ../ 越界
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        name = f"atlas-bench-verify-{uuid.uuid4().hex[:12]}"
        cmd = [
            "docker", "run", "--rm", "--name", name,
            "--network", "none", "--memory", "1g", "--cpus", "1",
            "-v", f"{ws}:/workspace", "-v", f"{Path(task.dir) / 'verifier'}:/verifier:ro",
            "-w", "/workspace", task.verifier.image, "sh", "-c", task.verifier.command,
        ]  # fmt: skip
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except OSError as exc:
            return VerifyResult(False, 0.0, "", infra_error=f"无法启动校验容器：{exc}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=task.verifier.timeout_s)
        except TimeoutError:
            with contextlib.suppress(Exception):
                kill = await asyncio.create_subprocess_exec("docker", "kill", name)
                await kill.wait()
            return VerifyResult(False, 0.0, f"校验超过 {task.verifier.timeout_s:.0f} 秒")
        text = out.decode(errors="replace")
        if proc.returncode == 125:  # docker 自身的错误（镜像、挂载），不是校验失败
            return VerifyResult(False, 0.0, text[-_TAIL:], infra_error="校验容器启动失败")
        passed = proc.returncode == 0
        return VerifyResult(passed, _score(text, passed), text[-_TAIL:])


def _tar(directory: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(directory.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(directory).as_posix())
    return buf.getvalue()


async def verify_in_sandbox(platform: AtlasAgentPlatform, tr: TrialRun, task: Task) -> VerifyResult:
    """sandbox 模式：verifier/ 打包送进沙箱、解到 dest，在 cwd 里执行 command，再读得分文件。

    ★ 会话没有正在运行的沙箱 = Agent 一条命令都没执行过：现场还是初始状态，记不通过
      （不是评测侧的问题）。执行途中沙箱没了、平台报错才算 infra_error。
    """
    from .platforms.atlas import AtlasError

    v = task.verifier
    staged = f"/tmp/atlas-bench-verifier-{uuid.uuid4().hex[:12]}.tar"
    dest, reward = shlex.quote(v.dest), shlex.quote(v.reward_file)
    command = (
        f"mkdir -p {dest} {shlex.quote(posixpath.dirname(v.reward_file))}"
        f" && tar -xf {staged} -C {dest} && rm -f {staged}"
        f" && {v.command}"
    )
    try:
        run = await platform.sandbox_exec(
            tr,
            command,
            cwd=v.cwd,
            timeout_s=v.timeout_s,
            files={staged: _tar(Path(task.dir) / "verifier")},
        )
        if run is None:
            return VerifyResult(False, 0.0, "会话没有正在运行的沙箱：Agent 没有执行过任何命令")
        got = await platform.sandbox_exec(tr, f"cat {reward}", cwd="/", timeout_s=60)
    except AtlasError as exc:
        return VerifyResult(False, 0.0, "", infra_error=f"沙箱内校验没做成：{exc}")
    output = str(run["output"])[-_TAIL:]
    if got is None or got["exit_code"] != 0:
        return VerifyResult(False, 0.0, f"{output}\n[没有得分文件 {v.reward_file}]")
    try:
        score = max(0.0, min(1.0, float(str(got["output"]).strip())))
    except ValueError:
        return VerifyResult(False, 0.0, f"{output}\n[得分文件不是数字：{got['output'][:100]!r}]")
    return VerifyResult(score >= 1.0, score, output)
