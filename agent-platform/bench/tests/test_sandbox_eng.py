"""沙箱工程用例的评测能力（doc/sandbox-eng-cases.html §4）。

计数、错误分类、故障注入、集群操作、多轮。

都对着假的东西跑：假 atlas-server（httpx.MockTransport）、假集群（记下 kubectl / ssh 命令）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_bench.agent.cluster import ClusterError, ClusterOps
from atlas_bench.agent.faults import FaultInjector
from atlas_bench.agent.marks import mark_report
from atlas_bench.agent.platforms.atlas import AtlasAgentPlatform, TrialRun
from atlas_bench.agent.report import hard_gate_failures, write_agent_report
from atlas_bench.agent.runner import _classify
from atlas_bench.agent.schema import (
    Action,
    AgentExperimentConfig,
    Task,
    TrialResult,
    VariantConfig,
)
from atlas_bench.agent.tasks import load_suite
from atlas_bench.agent.trajectory import metrics_from_trace
from atlas_bench.settings import BenchSettings

BENCH_HEADERS = {"X-Uid": "00000000-0000-0000-0000-0000000000be", "X-Caller-App": "bench"}
RUN = "0190a0a0-0000-7000-8000-000000000001"


def _started(call: str, command: str) -> dict[str, Any]:
    return {
        "type": "tool.started",
        "run_id": RUN,
        "data": {"call_id": call, "name": "execute", "args": {"command": command}},
    }


def _done(call: str, result: str, **artifact: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"call_id": call, "name": "execute", "result": result}
    if artifact:
        data["artifact"] = artifact
    return {"type": "tool.completed", "run_id": RUN, "data": data}


# ─────────────────────────────────────────────── 计数


def test_mark_report_counts_runs_reruns_tokens_and_hosts() -> None:
    files = {
        "marks/slow/start-1": b"host=sbx-a pid=1 at=1\n",
        "marks/slow/start-2": b"host=sbx-a pid=2 at=2\n",  # 平台重放时重新提交了一次
        "marks/slow/end-1": b"token=Tabc at=3\n",
        "marks/fast/start-1": b"host=sbx-b pid=3 at=1\n",
        "marks/fast/end-1": b"token=Tzzz at=2\n",
        "marks/mark.sh": b"#!/bin/sh\n",
        "other.txt": b"x",
    }
    trace = [
        _started("c1", "sh marks/mark.sh slow 90"),
        _done("c1", "tick 1\ntick 2\n42\nMARK-DONE slow Tabc\n[Command succeeded]"),
        _started("c2", "sh marks/mark.sh fast 3 && echo ok"),
        _done("c2", "tick 1\ntick 3\ntick 2\n"),  # 断线续读重了 / 乱了
    ]
    r = mark_report(files, trace)
    m = r.metrics
    assert (m["marks.slow.start"], m["marks.slow.end"], m["marks.slow.calls"]) == (2, 1, 1)
    assert m["marks.slow.token_seen"] == 1 and m["marks.slow.seq_broken"] == 0
    assert m["marks.fast.token_seen"] == 0 and m["marks.fast.seq_broken"] == 1
    assert m["platform_reruns"] == 1  # slow：两次执行、只有一个调用
    assert r.hosts == {"sbx-a", "sbx-b"} and m["sandbox_hosts"] == 2


def test_interrupted_and_unapproved_runs() -> None:
    files = {
        "marks/a/start-1": b"host=h", "marks/a/start-2": b"host=h", "marks/a/end-2": b"token=T",
        "marks/b/start-1": b"host=h",  # 被拒了却跑了
    }  # fmt: skip
    trace = [_started("c1", "sh marks/mark.sh a 9"), _started("c2", "sh marks/mark.sh a 9"),
             _started("c3", "sh marks/mark.sh b 0")]  # fmt: skip
    approvals = [
        {"command": "sh marks/mark.sh a 9", "decision": "approved"},
        {"command": "sh marks/mark.sh a 9", "decision": "approved"},
        {"command": "sh marks/mark.sh b 0", "decision": "rejected"},
    ]
    m = mark_report(files, trace, approvals).metrics
    assert m["marks.a.interrupted"] == 1 and m["marks.a.approvals"] == 2
    assert m["unapproved_runs"] == 1 and m["platform_reruns"] == 0


def test_labels_called_only_inside_a_script_are_not_compared() -> None:
    """sh steps.sh 里面调 mark.sh：命令文本里没有这个标记，执行次数无从和调用次数对照。"""
    files = {"marks/step2/start-1": b"host=h"}
    trace = [_started("c1", "sh steps.sh"), _started("c2", "cat marks/mark.sh 2>&1")]
    m = mark_report(files, trace).metrics
    assert m["platform_reruns"] == 0 and m["marks.step2.calls"] == 0
    assert not any(k.startswith("marks.2.") for k in m)


def test_a_model_decided_retry_is_not_a_platform_rerun() -> None:
    files = {"marks/x/start-1": b"host=h", "marks/x/start-2": b"host=h"}
    trace = [_started("c1", "sh marks/mark.sh x 1"), _started("c2", "sh marks/mark.sh x 1")]
    assert mark_report(files, trace).metrics["platform_reruns"] == 0


# ─────────────────────────────────────────────── 沙箱错误按属性分类


def test_sandbox_errors_are_counted_by_the_platform_attribute() -> None:
    def failed(result: str, kind: str | None) -> dict[str, Any]:
        data: dict[str, Any] = {"name": "execute", "result": result}
        if kind is not None:
            data["artifact"] = {"sandbox_error": kind}
        return {"type": "tool.failed", "data": data}

    trace = [
        failed("Error: Sandbox unavailable. 沙箱启动失败：配额", "unavailable"),
        failed("Error: Sandbox unavailable. 命令执行期间沙箱重启", "lost"),
        failed("Error: Sandbox unavailable. 提交命令时连接中断", "result_unknown"),
        failed("Error: Sandbox unavailable. 旧版本平台", None),  # 没有属性：按旧前缀退路
        failed("Error: Invalid parameter.", None),
        _done("c9", "[沙箱环境已重建：…]\nok", sandbox_rebuilt=True, exit_code=0),
    ]
    m = metrics_from_trace(trace, [])
    assert m["sandbox_unavailable"] == 2 and m["sandbox_lost"] == 1
    assert m["sandbox_result_unknown"] == 1 and m["sandbox_rebuilt"] == 1


def test_expected_sandbox_errors_are_not_platform_errors_when_faults_are_injected() -> None:
    tr = TrialRun(thread_id="t", status="succeeded")
    lost = {"sandbox_lost": 1}
    assert _classify(tr, False, lost) == "platform_error"  # 没注入故障却丢了沙箱：平台问题
    assert _classify(tr, False, lost, injected=True) == "failure"
    assert _classify(tr, True, lost, injected=True) == "success"


# ─────────────────────────────────────────────── 故障注入


class _FakeOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.replica_count = 1

    async def owner_pod(self, run_id: str) -> str:
        return "atlas-server-x"

    async def kill_server(self, pod: str) -> None:
        self.calls.append(("kill_server", pod))

    async def delete_sandbox(self, pod: str, *, graceful: bool = False) -> None:
        self.calls.append(("delete_sandbox_graceful" if graceful else "delete_sandbox", pod))

    async def kill_cli(self, pod: str, session: str) -> str:
        self.calls.append(("kill_cli", pod, session))
        return "123=node /opt/acp-cli/bin/claude-acp"

    async def pause(self, pod: str) -> None:
        self.calls.append(("pause", pod))

    async def resume(self, pod: str) -> None:
        self.calls.append(("resume", pod))

    async def block(self, pod: str) -> str:
        self.calls.append(("block", pod))
        return "bench-block-1"

    async def unblock(self, name: str) -> None:
        self.calls.append(("unblock", name))

    async def replicas(self) -> int:
        return self.replica_count

    async def scale(self, replicas: int) -> None:
        self.calls.append(("scale", str(replicas)))

    async def rollout_restart(self) -> None:
        self.calls.append(("restart",))


class _Site:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.missing_sandbox = False

    def current_run(self) -> str:
        return RUN

    def session(self) -> str:
        return "th-1"

    async def sandbox_pod(self, mark: str | None) -> str:
        if self.missing_sandbox:
            raise RuntimeError("找不到沙箱")
        return f"sbx-of-{mark}"

    async def cancel_run(self, run_id: str) -> None:
        self.cancelled.append(run_id)


def _injector(actions: list[dict[str, Any]], ops: Any = None, site: Any = None) -> FaultInjector:
    return FaultInjector(
        [Action.model_validate(a) for a in actions], ops or _FakeOps(), site or _Site()
    )


async def test_actions_fire_on_the_nth_matching_event_after_a_delay() -> None:
    ops = _FakeOps()
    inj = _injector(
        [
            {
                "do": "kill_server",
                "when": {"event": "tool.started", "mark": "slow"},
                "after_s": 0.05,
            },
            {
                "do": "delete_sandbox",
                "when": {"event": "tool.started", "tool": "execute", "nth": 2},
            },
        ],
        ops,
    )
    inj.start()
    inj.observe(_started("c0", "sh marks/mark.sh slowpoke 1"))  # 标记要整词匹配
    inj.observe({"type": "tool.started", "data": {"name": "read_file"}})
    assert ops.calls == [] and inj.pending() == 2
    inj.observe(_started("c1", "sh marks/mark.sh slow 90"))  # execute 第 2 次 + slow 第 1 次
    await inj.settle()
    assert ("kill_server", "atlas-server-x") in ops.calls
    assert ("delete_sandbox", "sbx-of-None") in ops.calls
    assert inj.pending() == 0 and all(r["ok"] for r in inj.records)
    inj.observe(_started("c2", "sh marks/mark.sh slow 90"))  # 只触发一次
    await inj.settle()
    assert len(ops.calls) == 2


async def test_injected_faults_are_always_restored() -> None:
    ops = _FakeOps()
    ops.replica_count = 2
    inj = _injector(
        [
            {"do": "pause_server"},
            {"do": "block_sandbox", "when": {"event": "tool.started", "mark": "net"}},
            {"do": "scale_lifecycle", "args": {"replicas": 0}},
        ],
        ops,
    )
    inj.start()
    inj.observe(_started("c1", "sh marks/mark.sh net 60"))
    await inj.close()  # trial 结束（成功、失败、超预算都走这里）
    assert ("resume", "atlas-server-x") in ops.calls
    assert ("unblock", "bench-block-1") in ops.calls
    scaled_down = ops.calls.index(("scale", "0"))
    assert ("scale", "2") in ops.calls[scaled_down + 1 :]  # 恢复成原来的副本数
    assert any(r["do"].startswith("restore:") for r in inj.records)


async def test_an_explicit_resume_is_not_undone_twice() -> None:
    ops = _FakeOps()
    inj = _injector(
        [{"do": "pause_server"}, {"do": "resume_server", "when": {"event": "run.resumed"}}], ops
    )
    inj.start()
    await inj.settle()
    inj.observe({"type": "run.resumed", "data": {"reason": "expired"}})
    await inj.close()
    assert ops.calls.count(("resume", "atlas-server-x")) == 1


async def test_a_deadline_fires_an_action_whose_trigger_never_comes() -> None:
    ops = _FakeOps()
    inj = _injector(
        [{"do": "resume_server", "when": {"event": "run.resumed"}, "deadline_s": 0.05}], ops
    )
    inj.start()
    await asyncio.sleep(0.1)
    await inj.settle()
    assert inj.pending() == 0 and inj.records[0]["do"] == "resume_server"


async def test_a_failed_injection_is_recorded_not_raised() -> None:
    site = _Site()
    site.missing_sandbox = True
    inj = _injector([{"do": "delete_sandbox"}], site=site)
    inj.start()
    await inj.close()
    assert inj.records[0]["ok"] is False and "找不到沙箱" in inj.records[0]["detail"]


async def test_cancel_run_targets_the_current_run_and_needs_no_cluster() -> None:
    site = _Site()
    inj = FaultInjector([Action(do="cancel_run")], None, site)
    inj.start()
    await inj.close()
    assert site.cancelled == [RUN] and inj.records[0]["ok"]


# ─────────────────────────────────────────────── 集群操作（记下命令，不真执行）


class _FakeCluster:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.argv: list[list[str]] = []
        self.stdin: list[str | None] = []

    async def __call__(self, argv: list[str], stdin: str | None, timeout: float) -> str:
        self.argv.append(argv)
        self.stdin.append(stdin)
        joined = " ".join(argv)
        for key, reply in self.replies.items():
            if key in joined:
                return reply
        return ""


def _pods(*names: str) -> str:
    return json.dumps({"items": [{"metadata": {"name": n}} for n in names]})


async def test_owner_pod_uses_the_only_replica_or_the_run_owner() -> None:
    one = _FakeCluster({"get pods": _pods("atlas-server-a")})
    assert await ClusterOps(BenchSettings(), run=one).owner_pod(RUN) == "atlas-server-a"
    assert not any("psql" in a for argv in one.argv for a in argv)

    two = _FakeCluster(
        {"get pods": _pods("atlas-server-a", "atlas-server-b"), "psql": "atlas-server-b-17-x9\n"}
    )
    assert await ClusterOps(BenchSettings(), run=two).owner_pod(RUN) == "atlas-server-b"
    with pytest.raises(ValueError):
        await ClusterOps(BenchSettings(), run=two).owner_pod("x'; drop table run; --")


async def test_pause_signals_the_container_process_on_the_node() -> None:
    fake = _FakeCluster(
        {"jsonpath={.status.containerStatuses": "containerd://0123456789abcdef", "crictl": "4242"}
    )
    await ClusterOps(BenchSettings(ssh_target="root@node"), run=fake).pause("atlas-server-a")
    assert fake.argv[-1][:4] == ["ssh", "-o", "BatchMode=yes", "-o"]
    assert fake.argv[-1][-1] == "kill -STOP 4242" and "root@node" in fake.argv[-1]


async def test_block_applies_a_deny_all_ingress_policy_for_that_sandbox() -> None:
    found = {"atlas-sbx get pod sbx-pod --ignore-not-found": "pod/sbx-pod"}
    fake = _FakeCluster(
        {**found, "jsonpath={.metadata.labels}": json.dumps({"atlas.sandbox": "sb-1"})}
    )
    name = await ClusterOps(BenchSettings(), run=fake).block("sbx-pod")
    policy = json.loads(fake.stdin[-1] or "")
    assert name == f"atlas-sbx/{policy['metadata']['name']}"
    assert policy["metadata"]["namespace"] == "atlas-sbx"
    assert policy["spec"] == {
        "podSelector": {"matchLabels": {"atlas.sandbox": "sb-1"}},
        "policyTypes": ["Ingress"],
    }
    unlabeled = _FakeCluster({**found, "jsonpath={.metadata.labels}": "{}"})
    with pytest.raises(ClusterError):
        await ClusterOps(BenchSettings(), run=unlabeled).block("sbx-pod")


# ─────────────────────────────────────────────── 适配器：多轮 + 取消动作


class _MultiTurnAtlas:
    """两轮：第一轮的 execute 开始后被评测取消，第二轮正常结束。"""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.cancelled: list[str] = []
        self.status: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        if (m, p) == ("POST", "/v1/threads"):
            return httpx.Response(201, json={"id": "th-1"})
        if m == "PUT":
            return httpx.Response(201, json={})
        if (m, p) == ("POST", "/v1/threads/th-1/runs"):
            rid = f"run-{len(self.runs) + 1}"
            self.runs.append(rid)
            self.status[rid] = "running" if rid == "run-1" else "succeeded"
            return httpx.Response(202, json={"run_id": rid})
        if (m, p) == ("GET", "/v1/threads/th-1/events"):
            ev = {
                "type": "tool.started",
                "run_id": "run-1",
                "data": {"call_id": "c1", "name": "execute",
                         "args": {"command": "sh marks/mark.sh cancel 120"}},
            }  # fmt: skip
            return httpx.Response(200, text=f"id: 5\ndata: {json.dumps(ev)}\n\n")
        if m == "POST" and p.endswith("/cancel"):
            rid = p.split("/")[3]
            self.cancelled.append(rid)
            self.status[rid] = "cancelled"
            return httpx.Response(200, json={})
        if m == "GET" and p.startswith("/v1/runs/") and p.count("/") == 3:
            return httpx.Response(200, json={"status": self.status[p.split("/")[3]]})
        if m == "GET" and p.endswith("/trace"):
            return httpx.Response(200, json={"data": [{"run_id": p.split("/")[3], "type": "x"}]})
        return httpx.Response(404, json={"detail": f"unexpected {m} {p}"})


async def test_turns_follow_the_previous_run_and_a_cancel_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_POLL_S", 0.05)
    fake = _MultiTurnAtlas()
    plat = AtlasAgentPlatform(
        "http://atlas", BENCH_HEADERS, transport=httpx.MockTransport(fake.handler)
    )
    task = Task(
        id="sandbox-eng/x",
        suite="sandbox-eng",
        instruction="run it",
        turns=[{"text": "again"}],
        actions=[{"do": "cancel_run", "when": {"event": "tool.started", "mark": "cancel"}}],
    )
    try:
        tr = await plat.run_trial({"id": "ag"}, task, trial=1, approval_policy="approve_all")
        trace, _ = await plat.collect(tr)
    finally:
        await plat.aclose()
    assert fake.cancelled == ["run-1"] and tr.run_ids == ["run-1", "run-2"]
    assert tr.run_id == "run-1" and tr.status == "succeeded" and tr.error is None
    assert [e["run_id"] for e in trace] == ["run-1", "run-2"]
    assert tr.actions[0]["do"] == "cancel_run" and tr.actions[0]["ok"]


# ─────────────────────────────────────────────── 任务加载与报告


def test_suite_shared_files_and_binary_files_are_loaded(tmp_path: Path) -> None:
    import atlas_bench.agent.tasks as tasks_mod

    suite = tmp_path / "s"
    (suite / "_shared" / "files" / "marks").mkdir(parents=True)
    (suite / "_shared" / "files" / "marks" / "mark.sh").write_text("shared")
    (suite / "t" / "files").mkdir(parents=True)
    (suite / "t" / "task.yaml").write_text("instruction: go\n")
    (suite / "t" / "files" / "blob.bin").write_bytes(b"\x00\xff\xfe")
    old = tasks_mod.TASK_DIRS
    tasks_mod.TASK_DIRS = [tmp_path]
    try:
        [task] = load_suite("s")
    finally:
        tasks_mod.TASK_DIRS = old
    assert task.files == {"marks/mark.sh": "shared", "blob.bin": b"\x00\xff\xfe"}


def test_known_defects_are_reported_but_not_gated(tmp_path: Path) -> None:
    cfg = AgentExperimentConfig(
        name="t",
        suites=["sandbox-chaos"],
        trials=1,
        variants={"a": VariantConfig(agent={"model": {"model": "m"}})},
    )
    tasks = [{"id": "c/known", "category": "eng", "tags": []},
             {"id": "c/fine", "category": "eng", "tags": []}]  # fmt: skip
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "config": cfg.model_dump(mode="json"),
                "git_commit": "abc",
                "started_at": "now",
                "tasks": tasks,
                "agents": {"a": {"slug": "bench-a"}},
            }
        )
    )
    (tmp_path / "trials").mkdir()
    rows = [
        TrialResult(task_id="c/known", variant="a", trial=1, outcome="failure",
                    expect_ok=False, known_defect="D1 交接中断命令",
                    expectations=[{"desc": "end=1", "ok": False, "gate": True}]),
        TrialResult(task_id="c/fine", variant="a", trial=1, outcome="success", passed=True,
                    expect_ok=True, actions=[{"do": "kill_server", "at_s": 10.0, "ok": True,
                                              "detail": "atlas-server-x", "desc": ""}]),
    ]  # fmt: skip
    (tmp_path / "trials" / "a.jsonl").write_text("\n".join(r.model_dump_json() for r in rows))
    md, _ = write_agent_report(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert hard_gate_failures(summary) == []
    text = md.read_text()
    assert "已知缺陷（不进硬门槛）" in text and "D1 交接中断命令" in text
    assert "故障注入" in text and "kill_server" in text


def test_actions_in_yaml_use_when_and_reject_unknown_keys() -> None:
    """YAML 1.1 把 on 读成布尔值 true：写成 on 的触发条件会被静默丢掉、动作开局就执行（踩过）。

    触发条件叫 when；未知的键（包括被读成 True 的 on）直接报错。
    """
    import yaml
    from pydantic import ValidationError

    ok = yaml.safe_load("do: kill_server\nwhen: {event: tool.started, mark: slow}\n")
    assert Action.model_validate(ok).when is not None
    bad = yaml.safe_load("do: kill_server\non: {event: tool.started}\n")
    assert True in bad  # 就是这个坑
    with pytest.raises(ValidationError):
        Action.model_validate(bad)
    for task in load_suite("sandbox-eng"):
        for action in task.actions:  # 套件里有触发条件的动作都真的解析出了触发条件
            assert action.when is not None or action.do == "scale_lifecycle", task.id
