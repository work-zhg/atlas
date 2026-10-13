"""校验（doc/agent-eval-design.html §11.1）：把最终工作区拷进隔离容器，跑任务自带的校验脚本。

★ 不在评测机本地直接跑：工作区里是模型写的代码，校验常常要执行它 —— 一律进容器，且无网络。
★ 校验脚本挂到 /verifier（只读），Agent 从头到尾看不到它。
★ 结果 = 退出码（0 通过）；脚本可在 stdout 最后一行写 `SCORE=<0~1>` 给部分得分。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .schema import Task

__all__ = ["VerifyResult", "verify_workspace"]

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
