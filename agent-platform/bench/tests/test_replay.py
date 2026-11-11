"""模型反应回放（agent/replay.py）：场景、平台原文对照、容器现场、端到端（假模型）。"""

from __future__ import annotations

import ast
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from atlas_bench.agent.replay import ReplayConfig, Scenario, load_scenarios, replay_once
from atlas_bench.settings import REPO_ROOT
from openai.types.chat import ChatCompletion

_IMAGE = "atlas-sandbox:dev"
_PREFIX = "Error: Sandbox unavailable. "
#: 平台给模型这些原文的源码（回放场景里写的原文要和它们一字不差）
_SOURCES = [
    REPO_ROOT / "agent-platform/libs/engine/src/atlas_engine/contracts/messages.py",
    REPO_ROOT / "agent-platform/services/agent/src/atlas_agent/sandbox/exec/__init__.py",
]


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True)
    return probe.returncode == 0


def _string_constants(path: Path) -> set[str]:
    """源码里的全部字符串常量（相邻的字面量在语法树里已经拼成一个）。"""
    return {
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_plan_scenarios_load() -> None:
    scenarios = load_scenarios("sandbox-plan")
    assert len(scenarios) == 9
    for sc in scenarios:
        assert sc.history and sc.history[-1].tool == "execute", sc.id
        assert (Path(sc.dir) / "verifier" / "verify.py").exists(), sc.id


def test_platform_texts_in_scenarios_match_the_platform_source() -> None:
    """平台改了给模型的原文，回放场景要跟着改 —— 否则测的就不是模型真正会看到的话。"""
    constants = set().union(*(_string_constants(p) for p in _SOURCES))
    for sc in load_scenarios("sandbox-plan"):
        for step in sc.history:
            if step.tool != "execute" or "[Command succeeded" in step.result:
                continue
            text = step.result.removeprefix(_PREFIX)
            if text.startswith("[沙箱环境已重建"):  # 重建提示后面跟着命令自己的输出
                text = text.split("\n", 1)[0] + "\n"
            assert any(text == c or text in c for c in constants), f"{sc.id}：{text[:40]}…"


# ─────────────────────────────────────────────── 容器里的现场（需要 docker）


@pytest.mark.skipif(not _docker_ok(), reason=f"需要 docker 与 {_IMAGE}")
@pytest.mark.parametrize("scenario", load_scenarios("sandbox-plan"), ids=lambda s: s.id)
def test_scenario_prepare_builds_the_moment_of_failure(scenario: Scenario) -> None:
    from atlas_bench.agent.replay import Box

    async def go() -> None:
        box = await Box.start(scenario, _IMAGE)
        await box.close()

    asyncio.run(go())


def _completion(content: str = "", calls: list[tuple[str, dict[str, Any]]] = ()) -> ChatCompletion:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{n}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for n, (name, args) in enumerate(calls)
        ]  # fmt: skip
    return ChatCompletion.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        }
    )


class _FakeLlm:
    """按脚本回复的模型：每次 create 吐下一条。"""

    def __init__(self, replies: list[ChatCompletion]) -> None:
        self._replies = list(replies)
        self.raw = self
        self.chat = self
        self.completions = self

    async def create(self, **kw: Any) -> ChatCompletion:
        return self._replies.pop(0)


@pytest.mark.skipif(not _docker_ok(), reason=f"需要 docker 与 {_IMAGE}")
@pytest.mark.parametrize(
    ("variant", "first", "blind"),
    [
        ("done", "grep -c R-1001 orders.csv", False),  # 先检查：已经有了，不再追加
        ("notdone", "echo 'R-1001,refund,-50' >> orders.csv", True),  # 没检查就重做（碰巧对）
    ],
)
def test_replay_runs_the_model_in_the_container_and_verifies(
    variant: str, first: str, blind: bool
) -> None:
    sc = next(s for s in load_scenarios("sandbox-plan") if s.id.endswith(f"append-{variant}"))
    llm = _FakeLlm([_completion(calls=[("execute", {"command": first})]), _completion("好了")])
    cfg = ReplayConfig(name="t", suites=["sandbox-plan"], model="fake", image=_IMAGE)
    r = asyncio.run(replay_once(llm, cfg, sc, 1))  # type: ignore[arg-type]
    assert r.error is None and r.verified and r.passed, r.verifier_output
    assert r.blind_retry is blind and r.steps == 1 and r.final == "好了"
