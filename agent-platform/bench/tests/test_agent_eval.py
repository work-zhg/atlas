"""智能体评测：任务集自检、过程指标、报告、Atlas 适配器（对着假的 atlas-server）。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_bench.agent.platforms.atlas import AtlasAgentPlatform
from atlas_bench.agent.report import write_agent_report
from atlas_bench.agent.schema import AgentExperimentConfig, Task, TrialResult, VariantConfig
from atlas_bench.agent.tasks import load_suite
from atlas_bench.agent.trajectory import metrics_from_trace
from atlas_bench.agent.verify import verify_workspace


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "python:3.12-slim"], capture_output=True)
    return r.returncode == 0


def test_demo_suite_loads() -> None:
    tasks = load_suite("demo")
    assert len(tasks) == 7
    ids = {t.id for t in tasks}
    assert "demo/fix-bug" in ids and "demo/wordfreq" in ids
    fix = next(t for t in tasks if t.id == "demo/fix-bug")
    assert set(fix.files) == {"calc.py", "test_calc.py"}
    for t in tasks:
        assert (Path(t.dir) / "verifier" / "verify.py").exists(), t.id
        # 校验脚本与参考答案不能进初始工作区（Agent 看得到 files/ 里的一切）
        assert not any(p.startswith(("verifier/", "solution/")) for p in t.files)


@pytest.mark.skipif(not _docker_ok(), reason="需要 docker 与 python:3.12-slim 镜像")
@pytest.mark.parametrize("task", load_suite("demo"), ids=lambda t: t.id)
def test_verifier_rejects_initial_and_accepts_solution(task: Task) -> None:
    """初始状态校验不过（题目有意义），参考答案校验通过（校验脚本没写错）。"""
    initial = {k: v.encode() for k, v in task.files.items()}
    bad = asyncio.run(verify_workspace(task, initial))
    assert bad.infra_error is None and not bad.passed, bad.output

    sol_dir = Path(task.dir) / "solution"
    solved = dict(initial)
    for p in sol_dir.rglob("*"):
        if p.is_file():
            solved[p.relative_to(sol_dir).as_posix()] = p.read_bytes()
    t = task
    if (sol_dir / "make.py").exists():
        cmd = f"python make.py && {task.verifier.command}"
        t = task.model_copy(update={"verifier": task.verifier.model_copy(update={"command": cmd})})
    good = asyncio.run(verify_workspace(t, solved))
    assert good.passed and good.score == 1.0, good.output


def test_metrics_from_trace() -> None:
    ev = [
        {"type": "tool.started", "data": {"name": "execute", "args": {"command": "ls"}}},
        {"type": "approval.required", "data": {}},
        {
            "type": "tool.completed",
            "data": {"name": "execute", "result": "x\n[Command failed with exit code 2]"},
        },
        {"type": "tool.started", "data": {"name": "execute", "args": {"command": "ls"}}},
        {"type": "tool.completed", "data": {"name": "execute", "result": "ok"}},
        {"type": "tool.started", "data": {"name": "read_file", "args": {"path": "a"}}},
        {"type": "tool.failed", "data": {"name": "read_file"}},
        {"type": "context.compacted", "data": {}},
    ]  # fmt: skip
    m = metrics_from_trace(ev, [{"total_tokens": 100, "input_tokens": 80}, {"total_tokens": 50}])
    assert m["tool_calls"] == 3 and m["shell_calls"] == 2 and m["shell_failures"] == 1
    assert m["tool_errors"] == 1 and m["repeated_calls"] == 1 and m["approvals"] == 1
    assert m["compactions"] == 1 and m["total_tokens"] == 150 and m["runs"] == 2
    assert m["by_tool"] == {"execute": 2, "read_file": 1}


def _cfg() -> AgentExperimentConfig:
    return AgentExperimentConfig(
        name="t",
        suites=["demo"],
        trials=2,
        variants={
            "a": VariantConfig(agent={"model": {"model": "m"}, "tool_names": ["bash"]}),
            "b": VariantConfig(agent={"model": {"model": "m"}}),
        },
        comparisons=[("a", "b")],
    )


def test_report_counts_infra_errors_separately(tmp_path: Path) -> None:
    cfg = _cfg()
    tasks = [
        {"id": "demo/x", "category": "code", "tags": []},
        {"id": "demo/y", "category": "data", "tags": []},
    ]
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "config": cfg.model_dump(mode="json"),
                "git_commit": "abc",
                "started_at": "now",
                "tasks": tasks,
                "agents": {"a": {"slug": "bench-a"}, "b": {"slug": "bench-b"}},
            }
        )
    )
    rows = {
        "a": [
            ("demo/x", 1, "success"),
            ("demo/x", 2, "success"),
            ("demo/y", 1, "failure"),
            ("demo/y", 2, "infra_error"),
        ],
        "b": [
            ("demo/x", 1, "failure"),
            ("demo/x", 2, "failure"),
            ("demo/y", 1, "failure"),
            ("demo/y", 2, "failure"),
        ],
    }
    (tmp_path / "trials").mkdir()
    for v, rs in rows.items():
        lines = [
            TrialResult(
                task_id=t, variant=v, trial=n, outcome=o, passed=o == "success",
                score=1.0 if o == "success" else 0.0, metrics={"tool_calls": 4, "tool_errors": 1},
            ).model_dump_json()
            for t, n, o in rs
        ]  # fmt: skip
        (tmp_path / "trials" / f"{v}.jsonl").write_text("\n".join(lines) + "\n")
    md, page = write_agent_report(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    a = summary["variants"]["a"]
    # x：2/2；y：1 次失败 + 1 次平台错误（不进分母）→ (1 + 0) / 2
    assert a["success"]["mean"] == 0.5 and a["outcomes"]["infra_error"] == 1
    assert a["pass_all_k"] == 0.5 and a["tool_error_rate"] == 0.25
    assert summary["comparisons"][0]["diff"]["mean"] == -0.5
    text = md.read_text()
    assert "主要结果" in text and "失败明细" in text and "平台错误" in text
    assert page.exists()


# ─────────────────────────────────────────────── Atlas 适配器


class _FakeAtlas:
    """最小的假 atlas-server。

    建智能体 / 会话 / 上传 / run（第二次轮询成功）/ 一次审批 / 轨迹 / 文件。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.uploads: dict[str, bytes] = {}
        self.approved: list[str] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        self.calls.append((m, p))
        if (m, p) == ("GET", "/v1/me"):
            return httpx.Response(200, json={"name": "atlas-bench", "kind": "service"})
        if (m, p) == ("GET", "/v1/agents"):
            return httpx.Response(200, json={"data": []})
        if (m, p) == ("POST", "/v1/agents"):
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": "ag-1", "slug": body["slug"], "version": 1})
        if (m, p) == ("POST", "/v1/agents/ag-1/status"):
            # 新建的智能体是草稿：适配器要把它启用（服务账号只能用已启用的）
            assert json.loads(request.content) == {"status": "enabled"}
            return httpx.Response(
                200, json={"id": "ag-1", "slug": "bench-native-x", "status": "enabled"}
            )
        if (m, p) == ("POST", "/v1/threads"):
            return httpx.Response(201, json={"id": "th-1"})
        if m == "PUT" and p == "/v1/threads/th-1/files":
            self.uploads[request.url.params["path"]] = request.content
            return httpx.Response(201, json={})
        if (m, p) == ("POST", "/v1/threads/th-1/runs"):
            assert request.headers.get("Idempotency-Key")
            return httpx.Response(
                202, json={"run_id": "run-1", "message_id": "m", "status": "queued"}
            )
        if (m, p) == ("GET", "/v1/threads/th-1/events"):
            ev = {
                "type": "approval.required",
                "run_id": "run-1",
                "data": {"approval_id": "ap-1", "tool_name": "execute"},
            }
            return httpx.Response(200, text=f"id: 3\ndata: {json.dumps(ev)}\n\n")
        if m == "POST" and p == "/v1/runs/run-1/approvals/ap-1":
            self.approved.append(json.loads(request.content)["decision"])
            return httpx.Response(200, json={})
        if (m, p) == ("GET", "/v1/runs/run-1"):
            self.polls += 1
            status = "succeeded" if self.polls >= 2 else "running"
            return httpx.Response(
                200, json={"status": status, "total_tokens": 10, "input_tokens": 8}
            )
        if (m, p) == ("GET", "/v1/runs/run-1/trace"):
            done = {"run_id": "run-1", "type": "message.completed", "depth": 0}
            return httpx.Response(200, json={"data": [{**done, "data": {"content": "done"}}]})
        if (m, p) == ("GET", "/v1/threads/th-1/files"):
            listing = [{"path": "out.txt", "size": 2}, {"path": "huge.bin", "size": 10**8}]
            return httpx.Response(200, json={"data": listing})
        if (m, p) == ("GET", "/v1/threads/th-1/files/download"):
            return httpx.Response(200, content=b"hi")
        return httpx.Response(404, json={"detail": f"unexpected {m} {p}"})


async def test_atlas_adapter_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_POLL_S", 0.05)
    fake = _FakeAtlas()
    plat = AtlasAgentPlatform("http://atlas", "k", transport=httpx.MockTransport(fake.handler))
    try:
        agent = await plat.prepare("native", VariantConfig(agent={"model": {"model": "m"}}))
        assert agent["slug"].startswith("bench-native-")
        task = Task(id="demo/x", suite="demo", instruction="do it", files={"a.py": "print(1)"})
        tr = await plat.run_trial(agent, task, trial=1, approval_policy="approve_all")
        trace, runs = await plat.collect(tr)
        files, skipped = await plat.download_workspace(tr)
    finally:
        await plat.aclose()
    assert ("POST", "/v1/agents/ag-1/status") in fake.calls
    assert tr.status == "succeeded" and tr.run_id == "run-1"
    assert fake.uploads == {"a.py": b"print(1)"}
    assert fake.approved == ["approved"] and tr.approvals[0]["tool"] == "execute"
    assert trace[0]["data"]["content"] == "done" and runs[0]["total_tokens"] == 10
    assert files == {"out.txt": b"hi"} and skipped == ["huge.bin"]


async def test_budget_exceeded_cancels_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_POLL_S", 0.05)
    fake = _FakeAtlas()
    fake.polls = -(10**6)  # 永远 running
    seen: list[Any] = []
    orig = fake.handler

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/runs/run-1/cancel":
            seen.append("cancel")
            return httpx.Response(200, json={})
        return orig(req)

    plat = AtlasAgentPlatform("http://atlas", "k", transport=httpx.MockTransport(handler))
    try:
        agent = await plat.prepare("v", VariantConfig(agent={}))
        task = Task(id="demo/x", suite="demo", instruction="x", budget={"wall_s": 0.2})
        tr = await plat.run_trial(agent, task, trial=1, approval_policy="approve_all")
    finally:
        await plat.aclose()
    assert tr.budget_exceeded and seen == ["cancel"]
