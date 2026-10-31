"""Terminal-Bench 2 接入：Harbor 任务的转换、在 Agent 用过的沙箱里原地校验。"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from atlas_bench.agent.platforms.atlas import AtlasAgentPlatform, TrialRun
from atlas_bench.agent.schema import Task
from atlas_bench.agent.verify import verify_in_sandbox
from atlas_bench.importers.tbench import convert_task

BENCH_HEADERS = {"X-Uid": "00000000-0000-0000-0000-0000000000be", "X-Caller-App": "bench"}

TASK_TOML = """
[metadata]
category = "system-administration"
difficulty = "medium"

[verifier]
timeout_sec = 300.0

[agent]
timeout_sec = 600.0

[environment]
docker_image = "alexgshaw/demo:20251031"
cpus = 1
memory_mb = 2048
"""


def _harbor_task(root: Path) -> Path:
    src = root / "demo"
    (src / "environment").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "solution").mkdir()
    (src / "task.toml").write_text(TASK_TOML)
    (src / "instruction.md").write_text("Fix the site.\n")
    (src / "environment" / "Dockerfile").write_text(
        "FROM ubuntu\nWORKDIR /app\nWORKDIR /app/site\n"
    )
    (src / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    (src / "tests" / "test_outputs.py").write_text("def test_ok(): pass\n")
    (src / "solution" / "solve.sh").write_text("git merge x\n")
    return src


def test_convert_task(tmp_path: Path) -> None:
    dst = tmp_path / "out" / "demo"
    convert_task(_harbor_task(tmp_path), dst)
    raw = yaml.safe_load((dst / "task.yaml").read_text())
    task = Task(id="tbench/demo", suite="tbench", dir=str(dst), **raw)
    # 起始目录 = 最后一个 WORKDIR，写进指令（Harbor 的 shell 本来就在那里）
    assert task.instruction == "Working directory: /app/site\n\nFix the site."
    assert task.verifier.mode == "sandbox" and task.verifier.cwd == "/app/site"
    assert task.verifier.timeout_s == 300.0 and task.budget.wall_s == 600.0
    assert task.agent_patch == {"sandbox": {"image": "alexgshaw/demo:20251031", "profile": "bench"}}
    assert task.category == "cli" and task.tags == ["tbench", "system-administration", "medium"]
    # tests/ → verifier/（Agent 看不到）；参考解原样保留；初始工作区为空（环境在镜像里）
    assert sorted(p.name for p in (dst / "verifier").iterdir()) == ["test.sh", "test_outputs.py"]
    assert (dst / "solution" / "solve.sh").exists()
    assert task.files == {}


# ─────────────────────────────────────────────── 原地校验


class _FakeExec:
    """假的 POST /v1/threads/th-1/sandbox/exec：第一次是校验命令，第二次读得分文件。"""

    def __init__(self, reward: dict[str, Any] | None, *, status: int = 200) -> None:
        self.reward = reward
        self.status = status
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/v1/threads/th-1/sandbox/exec")
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200:
            kind = "sandbox_not_running" if self.status == 409 else "dependency_unavailable"
            return httpx.Response(self.status, json={"error": {"kind": kind, "message": "x"}})
        if len(self.bodies) == 1:
            return httpx.Response(
                200, json={"exit_code": 0, "output": "2 passed", "truncated": False}
            )
        assert self.reward is not None
        return httpx.Response(200, json={**self.reward, "truncated": False})


async def _verify(tmp_path: Path, fake: _FakeExec) -> Any:
    dst = tmp_path / "out" / "demo"
    convert_task(_harbor_task(tmp_path), dst)
    task = Task(
        id="tbench/demo",
        suite="tbench",
        dir=str(dst),
        **yaml.safe_load((dst / "task.yaml").read_text()),
    )
    plat = AtlasAgentPlatform(
        "http://atlas", BENCH_HEADERS, transport=httpx.MockTransport(fake.handler)
    )
    try:
        return await verify_in_sandbox(plat, TrialRun(thread_id="th-1"), task)
    finally:
        await plat.aclose()


async def test_reward_one_passes_and_tests_arrive_only_now(tmp_path: Path) -> None:
    fake = _FakeExec({"exit_code": 0, "output": "1\n"})
    v = await _verify(tmp_path, fake)
    assert v.passed and v.score == 1.0 and v.infra_error is None
    run, read = fake.bodies
    assert run["cwd"] == "/app/site" and run["timeout_s"] == 300
    # 校验脚本随这次请求才送进沙箱：打包写到 /tmp，解到 /tests 再执行
    [staged] = run["files"]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(staged["content_b64"]))) as tar:
        assert sorted(tar.getnames()) == ["test.sh", "test_outputs.py"]
    path = staged["path"]
    assert run["command"] == (
        f"mkdir -p /tests /logs/verifier && tar -xf {path} -C /tests && rm -f {path}"
        " && bash /tests/test.sh"
    )
    assert read["command"] == "cat /logs/verifier/reward.txt"


@pytest.mark.parametrize(
    ("reward", "score"),
    [({"exit_code": 0, "output": "0"}, 0.0), ({"exit_code": 1, "output": "cat: no such"}, 0.0)],
)
async def test_zero_or_missing_reward_fails(
    tmp_path: Path, reward: dict[str, Any], score: float
) -> None:
    v = await _verify(tmp_path, _FakeExec(reward))
    assert not v.passed and v.score == score and v.infra_error is None


async def test_no_running_sandbox_means_the_agent_did_nothing(tmp_path: Path) -> None:
    """Agent 一条命令都没执行过：现场还是初始状态 —— 不通过，不是评测侧的问题。"""
    v = await _verify(tmp_path, _FakeExec(None, status=409))
    assert not v.passed and v.infra_error is None


async def test_sandbox_lost_during_verification_is_an_infra_error(tmp_path: Path) -> None:
    v = await _verify(tmp_path, _FakeExec(None, status=503))
    assert not v.passed and v.infra_error


async def test_connection_drop_during_verification_is_an_infra_error(tmp_path: Path) -> None:
    """校验请求断在半路（端口转发重连）：记平台错误、续跑时重来；不重发（校验脚本会再跑一遍）。"""
    fake = _FakeExec(None)

    def handler(request: httpx.Request) -> httpx.Response:
        fake.bodies.append({})
        raise httpx.ReadError("connection reset", request=request)

    fake.handler = handler  # type: ignore[method-assign]
    v = await _verify(tmp_path, fake)
    assert not v.passed and v.infra_error and len(fake.bodies) == 1
