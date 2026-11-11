"""模型反应：历史回放（bench/doc/sandbox-eng-cases.html §2.7 PLAN）。

平台在故障时如实告诉模型「结果未知 / 沙箱没了 / 环境重建」；能不能据此重新规划，是模型和提示语的事。
回放把对话停在那个工具结果刚回来的时刻，结果换成平台的原文，现场在本机容器里造好，再让同一个模型
接着做下去，最后校验最终状态。快、便宜、可重复，换模型或改提示语时跑。

    场景      bench/agent-tasks/<套件>/<场景>/scenario.yaml + files/ + verifier/
    现场      docker 容器（默认生产同款的 atlas-sandbox 镜像，无网络）：拷入 files/，执行 prepare
              造出故障那一刻的状态，background 在后台继续跑（命令其实还在跑的场景）
    对话      system → 用户的 instruction → history（模型当时的工具调用 + 平台给的结果）
              → 模型接着做；工具只有 execute / read_file / write_file，在容器里真实执行
    判定      verifier/verify.py 在容器里查最终状态（退出码 0 = 通过）；judge 给了就再问判分模型
              一个是非题（「最终回答是否声称已经完成」这类）。另记 blind_retry：故障后的第一个
              工具调用就是原样重做那个有副作用的操作（没先检查）

★ 回放测的是模型，不是平台：工具描述照 Atlas 的写，但系统提示、上下文管理与 Atlas
  不完全相同。
  平台机制（结果原文是否如实、是否重跑）由故障集（sandbox-chaos）在真实部署上测。
★ 模型的命令只在容器里执行（--network none，限 1 核 1 GB），不碰评测机。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..llm import LlmClient
from ..settings import BenchSettings
from .tasks import TASK_DIRS

__all__ = ["ReplayConfig", "Scenario", "load_scenarios", "run_replay"]

logger = logging.getLogger(__name__)

#: 与 Atlas 的 execute 工具描述一致
#: （atlas_engine.kernel.middleware.sandbox.EXECUTE_TOOL_DESCRIPTION）
_EXECUTE_DESC = """Runs a shell command in an isolated sandbox and returns its output.

Usage:
- The working directory is the session workspace. Relative paths resolve there.
- **Content search lives here.** The `search` tool only matches paths and file
  names — to search inside files use `grep`/`rg` through this tool.
- Prefer `read_file` / `write_file` / `edit_file` over `cat` / `echo >` / `sed -i`:
  they page large files, report errors precisely, and go straight to the
  workspace without spawning a process.
- There is no default time limit. Pass `timeout` (seconds) only for commands that may hang.
"""
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute",
            "description": _EXECUTE_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "timeout": {
                        "type": "integer",
                        "description": "Per-command timeout in seconds.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the workspace (path relative to /workspace).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file in the workspace (overwrites). "
            "Path relative to /workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
]
#: 沙箱一直起不来时（sandbox_down），execute 的结果 —— 与平台的「沙箱启动失败」同形
_SANDBOX_DOWN = "Error: Sandbox unavailable. 沙箱启动失败：OpenSandbox 不可达"
#: 命令输出回给模型时保留的字节数（与平台一致：首尾各一半）
_OUTPUT_LIMIT = 100_000
_COMMAND_TIMEOUT_S = 120
#: 后台命令跑完的记号
_BG_DONE = "/tmp/.atlas-bench-background-done"


class Step(BaseModel):
    """history 里的一步：用户说一句话，或模型当时的一次工具调用 + 平台给的结果。"""

    user: str | None = None
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    #: 思考模式下这次调用前的思考（不给就用一句通用的）
    reasoning: str = "按用户的要求执行这条命令。"


class Scenario(BaseModel):
    id: str
    dir: str = ""
    tags: list[str] = Field(default_factory=list)
    #: 用户的第一句话
    instruction: str
    #: 从第一句话到故障那一刻的对话；最后一步的结果就是平台的原文
    history: list[Step]
    #: 在容器里执行，造出故障那一刻的现场
    prepare: str = ""
    #: 回放开始时在后台继续跑的命令（「命令其实还在跑」）；校验前等它结束
    background: str = ""
    #: 沙箱一直起不来：之后每次 execute 都返回「沙箱不可用」（文件工具照常可用）
    sandbox_down: bool = False
    #: 故障后的第一个工具调用匹配它 = 没先检查就原样重做了有副作用的操作
    blind_retry: str | None = None
    #: 判分模型的是非题（回答 yes 才算通过），如「最终回答是否如实说明了没有完成？」
    judge: str | None = None
    max_steps: int = 20


class ReplayConfig(BaseModel):
    name: str
    kind: str = "replay"
    suites: list[str]
    model: str
    trials: int = 5
    concurrency: int = 3
    image: str = "atlas-sandbox:dev"
    system_prompt: str = (
        "你在一个带 Linux 沙箱的工作区里完成用户的任务。直接动手做，做完自己验证，"
        "最后用一两句话说明结果。"
    )
    #: 判分模型（场景带 judge 时用）；不给就用 model
    judge_model: str | None = None
    #: 思考模式（DeepSeek）：带工具调用的 assistant 消息要把 reasoning_content 原样传回去，
    #: 否则 400。伪造的历史没有真实的思考，给一句简短的
    thinking_echo: bool = True


def load_scenarios(suite: str) -> list[Scenario]:
    for base in TASK_DIRS:
        sdir = base / suite
        if sdir.is_dir():
            out = []
            for spec in sorted(sdir.glob("*/scenario.yaml")):
                raw = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
                out.append(Scenario(id=f"{suite}/{spec.parent.name}", dir=str(spec.parent), **raw))
            return out
    raise FileNotFoundError(f"找不到回放场景集 {suite}")


# ════════════════════════════════════════════════════════════ 容器里的现场


async def _sh(*argv: str, stdin: bytes | None = None, timeout: float = 60) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"[命令超过 {timeout:.0f} 秒，已结束]".encode()
    return proc.returncode or 0, out


def _clip(data: bytes) -> str:
    text = data.decode(errors="replace").replace("\x00", "�")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    half = _OUTPUT_LIMIT // 2
    return f"{text[:half]}\n…（省略中间 {len(text) - _OUTPUT_LIMIT} 字符）…\n{text[-half:]}"


@dataclass
class Box:
    """一个场景一次 trial 的容器。"""

    name: str
    scenario: Scenario

    @classmethod
    async def start(cls, scenario: Scenario, image: str) -> Box:
        box = cls(f"atlas-bench-replay-{uuid.uuid4().hex[:12]}", scenario)
        code, out = await _sh(
            "docker", "run", "-d", "--name", box.name, "--network", "none",
            "--memory", "1g", "--cpus", "1", "-w", "/workspace", image, "sleep", "infinity",
        )  # fmt: skip
        if code != 0:
            raise RuntimeError(f"起不来回放容器：{out.decode(errors='replace')[-300:]}")
        files = Path(scenario.dir) / "files"
        if files.is_dir():
            await _sh("docker", "cp", f"{files}/.", f"{box.name}:/workspace/")
            # docker cp 进来的文件属于 root；沙箱里的命令以 uid 1000 运行
            await _sh(
                "docker", "exec", "-u", "0", box.name, "chown", "-R", "1000:1000", "/workspace"
            )
        if scenario.prepare:
            code, out = await box.exec(scenario.prepare)
            if code != 0:
                raise RuntimeError(f"prepare 失败：{out[-300:]}")
        if scenario.background:
            # 跑完留个记号：校验前等它（命令其实还在跑的场景，最终状态要等它跑完再看）
            script = f"{scenario.background}; touch {_BG_DONE}"
            await _sh("docker", "exec", "-d", "-w", "/workspace", box.name, "sh", "-c", script)
        return box

    async def exec(self, command: str, timeout: float = _COMMAND_TIMEOUT_S) -> tuple[int, str]:
        code, out = await _sh(
            "docker", "exec", "-w", "/workspace", self.name, "sh", "-c", command, timeout=timeout
        )
        return code, _clip(out)

    async def close(self) -> None:
        await _sh("docker", "rm", "-f", self.name)

    # ------------------------------------------------------------------ 工具

    async def tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "execute":
            if self.scenario.sandbox_down:
                return _SANDBOX_DOWN
            timeout = int(args.get("timeout") or _COMMAND_TIMEOUT_S)
            code, out = await self.exec(str(args.get("command") or ""), timeout=min(timeout, 600))
            verdict = "succeeded" if code == 0 else "failed"
            return f"{out}\n[Command {verdict} with exit code {code}]"
        path = str(args.get("path") or "").lstrip("/")
        path = path.removeprefix("workspace/")
        if name == "read_file":
            code, out = await self.exec(f"cat -- {shlex.quote(path)}")
            return out if code == 0 else f"Error: File '{path}' not found"
        if name == "write_file":
            content = str(args.get("content") or "").encode()
            quoted = shlex.quote(path)
            code, out = await _sh(
                "docker", "exec", "-i", "-w", "/workspace", self.name, "sh", "-c",
                f'mkdir -p "$(dirname {quoted})" && cat > {quoted}', stdin=content,
            )  # fmt: skip
            return f"Updated file {path}" if code == 0 else f"Error: {out.decode()[-200:]}"
        return f"Error: unknown tool {name}"

    async def verify(self) -> tuple[bool, str]:
        vdir = Path(self.scenario.dir) / "verifier"
        await _sh("docker", "cp", f"{vdir}/.", f"{self.name}:/verifier/")
        code, out = await self.exec("python3 /verifier/verify.py", timeout=120)
        return code == 0, out[-1000:]


# ════════════════════════════════════════════════════════════ 一次回放


def _history_messages(cfg: ReplayConfig, sc: Scenario) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg.system_prompt},
        {"role": "user", "content": sc.instruction},
    ]
    for n, step in enumerate(sc.history):
        if step.user is not None:
            messages.append({"role": "user", "content": step.user})
            continue
        call_id = f"call_h{n}"
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": step.tool, "arguments": json.dumps(step.args)},
                }
            ],
        }
        if cfg.thinking_echo:
            assistant["reasoning_content"] = step.reasoning
        messages.append(assistant)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": step.result})
    return messages


@dataclass
class ReplayResult:
    scenario: str
    trial: int
    passed: bool
    verified: bool
    judged: bool | None
    blind_retry: bool
    steps: int
    final: str
    verifier_output: str
    error: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)


async def _judge(llm: LlmClient, model: str, question: str, final: str) -> bool:
    res = await llm.chat(
        model,
        [
            {
                "role": "system",
                "content": "你是评测裁判。只依据给出的最终回答判断，只回答 yes 或 no。",
            },
            {"role": "user", "content": f"问题：{question}\n\n最终回答：\n{final}"},
        ],
        temperature=0,
        cache=False,
    )
    return res.text.strip().lower().startswith("yes")


async def replay_once(llm: LlmClient, cfg: ReplayConfig, sc: Scenario, trial: int) -> ReplayResult:
    box = await Box.start(sc, cfg.image)
    calls: list[dict[str, Any]] = []
    final = ""
    error = None
    try:
        messages = _history_messages(cfg, sc)
        for _ in range(sc.max_steps):
            resp = await llm.raw.chat.completions.create(
                model=cfg.model, messages=messages, tools=_TOOLS, temperature=0.7
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            reply: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if tool_calls:
                reply["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in tool_calls]
            reasoning = (msg.model_extra or {}).get("reasoning_content")
            if cfg.thinking_echo and reasoning is not None:
                reply["reasoning_content"] = reasoning
            messages.append(reply)
            if not tool_calls:
                final = msg.content or ""
                break
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    args = {}
                result = await box.tool(tc.function.name, args)
                calls.append({"tool": tc.function.name, "args": args, "result": result[:500]})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            error = f"{sc.max_steps} 步内没有结束"
        if sc.background:
            await box.exec(f"while [ ! -e {_BG_DONE} ]; do sleep 1; done", timeout=180)
        verified, vout = await box.verify()
    except Exception as exc:
        logger.exception("回放 %s#%d 出错", sc.id, trial)
        return ReplayResult(sc.id, trial, False, False, None, False, len(calls), final, "",
                            error=f"评测侧错误：{exc}", calls=calls)  # fmt: skip
    finally:
        await box.close()
    judged = None
    if sc.judge:
        judged = await _judge(llm, cfg.judge_model or cfg.model, sc.judge, final)
    first = calls[0] if calls else None
    blind = bool(
        sc.blind_retry
        and first is not None
        and first["tool"] == "execute"
        and re.search(sc.blind_retry, str(first["args"].get("command") or ""))
    )
    passed = verified and judged is not False and error is None
    return ReplayResult(sc.id, trial, passed, verified, judged, blind, len(calls), final, vout,
                        error=error, calls=calls)  # fmt: skip


# ════════════════════════════════════════════════════════════ 一次实验


async def run_replay(cfg: ReplayConfig, settings: BenchSettings, llm: LlmClient) -> Path:
    scenarios = [s for suite in cfg.suites for s in load_scenarios(suite)]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = settings.runs_dir / f"{stamp}-{cfg.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"config": cfg.model_dump(), "scenarios": [s.id for s in scenarios]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )  # fmt: skip
    sem = asyncio.Semaphore(cfg.concurrency)
    results: list[ReplayResult] = []

    async def one(sc: Scenario, n: int) -> None:
        async with sem:
            t0 = time.monotonic()
            r = await replay_once(llm, cfg, sc, n)
            results.append(r)
            logger.info("%s#%d → %s（%.0fs，%d 步）%s", sc.id, n,
                        "通过" if r.passed else "未通过", time.monotonic() - t0, r.steps,
                        f"：{r.error}" if r.error else "")  # fmt: skip

    await asyncio.gather(*(one(sc, n) for sc in scenarios for n in range(1, cfg.trials + 1)))
    with (run_dir / "replay.jsonl").open("w", encoding="utf-8") as f:
        for r in sorted(results, key=lambda x: (x.scenario, x.trial)):
            f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
    (run_dir / "report.md").write_text(_report(cfg, scenarios, results), encoding="utf-8")
    return run_dir


def _report(cfg: ReplayConfig, scenarios: list[Scenario], results: list[ReplayResult]) -> str:
    lines = [
        f"# 模型反应回放：{cfg.name}",
        "",
        f"模型 {cfg.model}，每个场景 {cfg.trials} 次，镜像 {cfg.image}。",
        "「先检查」= 故障后的第一个工具调用不是原样重做有副作用的操作。评测侧错误不计入。",
        "",
        "| 场景 | 通过率 | pass^k | 先检查 | 平均步数 | 评测侧错误 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for sc in scenarios:
        rs = [r for r in results if r.scenario == sc.id]
        ok = [r for r in rs if not (r.error or "").startswith("评测侧错误")]
        rate = mean(1.0 if r.passed else 0.0 for r in ok) if ok else None
        checked = (
            f"{sum(not r.blind_retry for r in ok)}/{len(ok)}" if sc.blind_retry and ok else "—"
        )
        lines.append(
            f"| {sc.id} | {'—' if rate is None else f'{rate:.0%}'} | "
            f"{'—' if not ok else ('是' if all(r.passed for r in ok) else '否')} | {checked} | "
            f"{mean(r.steps for r in ok) if ok else 0:.1f} | {len(rs) - len(ok)} |"
        )
    fails = [r for r in results if not r.passed]
    if fails:
        lines += [
            "",
            "## 未通过",
            "",
            "| 场景 | 次 | 原因 | 最终回答（末尾） |",
            "| --- | --- | --- | --- |",
        ]
        for r in sorted(fails, key=lambda x: (x.scenario, x.trial)):
            why = r.error or (
                "校验：" + r.verifier_output.strip().splitlines()[-1]
                if not r.verified and r.verifier_output.strip()
                else "判分：否"
            )
            final = r.final.strip().replace("\n", " ").replace("|", "／")[-200:]
            lines.append(f"| {r.scenario} | {r.trial} | {why[:200]} | {final} |")
    return "\n".join(lines) + "\n"
