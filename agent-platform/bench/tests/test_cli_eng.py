"""CLI 智能体功能用例的评测能力（doc/cli-func-cases.html §4）。

计数认 CLI 的命令、触发条件认参数补全、事件级不变量、按轮声明的预期终态、审批提交、
会话 Pod 的集群操作与回收检查、套件共享的断言。

都对着假的东西跑：假 atlas-server（httpx.MockTransport）、假集群（记下 kubectl 命令）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_bench.agent.cluster import ClusterOps
from atlas_bench.agent.faults import FaultInjector
from atlas_bench.agent.marks import mark_report
from atlas_bench.agent.platforms.atlas import AtlasAgentPlatform, TrialRun
from atlas_bench.agent.runner import _reclaim, _workspace_changes
from atlas_bench.agent.schema import Action, RunExpect, Task
from atlas_bench.agent.tasks import load_suite
from atlas_bench.agent.trajectory import event_invariants
from atlas_bench.settings import BenchSettings

BENCH_HEADERS = {"X-Uid": "00000000-0000-0000-0000-0000000000be", "X-Caller-App": "bench"}


def _ev(kind: str, run: str = "r1", seq: int | None = None, **data: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"type": kind, "run_id": run, "data": data}
    if seq is not None:
        e["seq"] = seq
    return e


# ─────────────────────────────────────────────── 计数认 CLI 的命令


def test_marks_follow_the_command_through_the_cli_refinement_not_the_tool_name() -> None:
    """CLI 先报工具名（命令第一行、参数为空），命令随后用 tool.updated 补全；
    审批的参数在 rawInput 下。"""
    files = {
        "marks/slow/start-1": b"host=cli-pod-a pid=1 at=1\n",
        "marks/slow/end-1": b"token=Tabc at=3\n",
        "marks/once/start-1": b"host=cli-pod-a pid=2 at=1\n",  # 被拒了却跑了
    }
    trace = [
        _ev("tool.started", call_id="toolu_1", name="sh marks/mark.sh slow 90", args={}),
        _ev("tool.updated", call_id="toolu_1", args={"command": "sh marks/mark.sh slow 90"}),
        _ev("tool.completed", call_id="toolu_1", result="tick 1\nMARK-DONE slow Tabc"),
        _ev("tool.started", call_id="toolu_2", name="Terminal", args={}),
        _ev("tool.failed", call_id="toolu_2", args={"command": "sh marks/mark.sh once 0"}),
    ]
    approvals = [
        {"command": "sh marks/mark.sh once 0", "decision": "rejected", "kind": "execute"},
    ]
    m = mark_report(files, trace, approvals).metrics
    assert m["marks.slow.calls"] == 1 and m["marks.slow.token_seen"] == 1
    assert m["marks.once.calls"] == 1 and m["marks.once.approvals"] == 1
    assert m["platform_reruns"] == 0 and m["unapproved_runs"] == 1
    assert m["sandbox_hosts"] == 1


class _Ops:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def kill_server(self, pod: str) -> None:
        self.calls.append(("kill_server", pod))

    async def owner_pod(self, run_id: str) -> str:
        return "atlas-server-x"

    async def delete_sandbox(self, pod: str, *, graceful: bool = False) -> None:
        self.calls.append(("delete_sandbox", pod, "graceful" if graceful else "force"))

    async def kill_cli(self, pod: str, session: str) -> str:
        self.calls.append(("kill_cli", pod, session))
        return "42=node /opt/acp-cli/bin/claude-acp"


class _Site:
    def current_run(self) -> str:
        return "r1"

    def session(self) -> str:
        return "th-1"

    async def sandbox_pod(self, mark: str | None) -> str:
        return f"cli-pod-of-{mark}"

    async def cancel_run(self, run_id: str) -> None:
        pass


async def test_a_mark_trigger_fires_on_the_refinement_and_counts_each_call_once() -> None:
    ops = _Ops()
    inj = FaultInjector(
        [
            Action.model_validate(
                {"do": "kill_server", "when": {"event": "tool.started", "mark": "slow", "nth": 2}}
            ),
            Action.model_validate(
                {"do": "kill_cli", "when": {"event": "tool.started", "mark": "crs"}}
            ),
            Action.model_validate(
                {
                    "do": "delete_sandbox",
                    "args": {"graceful": True, "mark": "crs"},
                    "when": {"event": "tool.started", "mark": "crs"},
                }
            ),
        ],
        ops,  # type: ignore[arg-type]
        _Site(),
    )
    inj.start()
    cmd = {"command": "sh marks/mark.sh slow 90"}
    inj.observe(_ev("tool.started", call_id="c1", name="Terminal", args={}))
    inj.observe(_ev("tool.updated", call_id="c1", args=cmd))
    inj.observe(_ev("tool.updated", call_id="c1", args=cmd, name="sh marks/mark.sh slow 90"))
    await inj.settle()
    assert ops.calls == []  # 同一个调用补全两次只算一次：nth=2 还没到
    inj.observe(_ev("tool.started", call_id="c2", name="x", args=cmd))
    inj.observe(_ev("tool.updated", call_id="c3", args={"command": "sh marks/mark.sh crs 90"}))
    await inj.settle()
    assert ("kill_server", "atlas-server-x") in ops.calls
    assert ("kill_cli", "cli-pod-of-crs", "th-1") in ops.calls
    assert ("delete_sandbox", "cli-pod-of-crs", "graceful") in ops.calls
    await inj.close()
    assert all(r["ok"] for r in inj.records)


# ─────────────────────────────────────────────── 事件级不变量


def test_event_invariants_catch_what_the_cli_translation_must_not_do() -> None:
    stream = [
        # r1：正文一致；seq 连续
        _ev("run.started", "r1", 1, permission_mode="auto"),
        _ev("message.delta", "r1", 2, text="第 1 段"),
        _ev("message.delta", "r1", 3, text="第 2 段"),
        _ev("message.completed", "r1", 4, content=[{"type": "text", "text": "第 1 段第 2 段"}]),
        _ev("run.finished", "r1", 5),
        # r2：接管后翻译器从空开始（CD3）—— 落库的回答缺前面那段；seq 有缺口
        _ev("message.delta", "r2", 1, text="接管前"),
        _ev("message.delta", "r2", 3, text="接管后"),
        _ev("message.completed", "r2", 4, content=[{"type": "text", "text": "接管后"}]),
        _ev("run.finished", "r2", 4),  # seq 重复
        # r3：流上没见到终止事件：不判它
        _ev("message.completed", "r3", 1, content="x"),
    ]
    for i, e in enumerate(stream, 1):
        e["thread_seq"] = i
    stream.append(dict(stream[1]))  # 断线重连重复收到同一条（同一个会话序号）：不算
    trace = [
        _ev("run.started", "r1", permission_mode="auto"),
        _ev("tool.started", "r1", call_id="c1", name="ls\nrm -rf x"),  # 多行名字
        _ev("tool.failed", "r1", call_id="c1", result="用户拒绝了这次调用。"),  # 没带 error
        _ev("tool.failed", "r1", call_id="c1", error="denied"),  # 第二条终态（CD10）
        _ev("tool.started", "r1", call_id="c2", name="Read"),  # 正常结束的 run 里没有终态
        _ev("usage.updated", "r1", input_tokens=10, cache_read=20),  # 没带模型名、口径不对
        _ev("approval.required", "r1", approval_id="a1"),
        _ev("approval.required", "r1", approval_id="a1"),  # 接管后再发一次（CD6）
        _ev("todos.updated", "r1", todos=[{"content": "x", "status": "in_progress"}]),
        _ev("run.finished", "r1"),
        _ev("tool.started", "r2", call_id="c1", name="ls"),  # 旧调用被下一轮重放
        _ev("tool.completed", "r2", call_id="c1"),
        _ev("subagent.started", "r2"),
    ]
    runs = [
        {"id": "r1", "status": "succeeded", "total_tokens": 5},
        {"id": "r2", "status": "succeeded", "total_tokens": 0},
        {"id": "r3", "status": "running"},
    ]
    m = event_invariants(trace, stream, runs)
    assert m["answer_mismatch"] == 1  # r2
    assert m["seq_gaps"] == 1 and m["seq_dups"] == 1  # r2 缺 2、4 重复
    assert m["stream_incomplete"] == 1  # r3
    assert m["tool_name_bad"] == 1 and m["tool_failed_no_error"] == 1
    assert m["tool_terminal_dup"] == 1 and m["tool_terminal_missing"] == 1
    assert m["tool_started_dup"] == 0 and m["call_ids_reused"] == 1
    assert m["cli_runs"] == 1 and m["usage_no_model"] == 1 and m["usage_cache_over_input"] == 1
    assert m["cli_runs_no_context_usage"] == 1
    assert m["runs_nonterminal"] == 1 and m["runs_zero_usage"] == 1
    assert m["approval_events"] == 2 and m["approval_dup"] == 1
    assert m["todos_open_last"] == 1 and m["subagents_unfinished"] == 1


def test_a_clean_cli_run_has_no_violations() -> None:
    events = [
        _ev("run.started", "r1", 1, permission_mode="auto"),
        _ev("tool.started", "r1", 2, call_id="c1", name="Terminal", args={}),
        _ev("tool.updated", "r1", 3, call_id="c1", name="sh marks/mark.sh a 0"),
        _ev("tool.completed", "r1", 4, call_id="c1"),
        _ev("message.delta", "r1", 5, text="好了"),
        _ev("context.usage", "r1", 6, basis="cli", tokens=100),
        _ev("message.completed", "r1", 7, content=[{"type": "text", "text": "好了"}]),
        _ev("usage.updated", "r1", 8, input_tokens=30, cache_read=20, model="deepseek-v4-pro"),
        _ev("run.finished", "r1", 9),
    ]
    for i, e in enumerate(events, 1):
        e["thread_seq"] = i
    archived = [e for e in events if e["type"] != "message.delta"]
    m = event_invariants(archived, events, [{"id": "r1", "status": "succeeded", "total_tokens": 9}])
    assert {k: v for k, v in m.items() if v and k != "cli_runs"} == {}


# ─────────────────────────────────────────────── 适配器：预期终态、审批提交、事件留存


#: CLI 的审批：参数是 ACP 的 toolCall，命令在 rawInput 里
CLI_ASK = {"toolCallId": "c1", "kind": "execute", "rawInput": {"command": "sh marks/mark.sh x 0"}}


class _CliAtlas:
    """两轮：第一轮以 bridge_lost 失败（任务声明了预期），第二轮成功。会话流上有一个 CLI 的审批：
    参数在 rawInput；评测要等一会儿才批，提交时已经被处理过（409）。"""

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.decided: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        if (m, p) == ("POST", "/v1/threads"):
            return httpx.Response(201, json={"id": "th-1"})
        if m == "PUT":
            return httpx.Response(201, json={})
        if (m, p) == ("POST", "/v1/threads/th-1/runs"):
            rid = f"run-{len(self.runs) + 1}"
            self.runs.append(rid)
            return httpx.Response(202, json={"run_id": rid})
        if (m, p) == ("GET", "/v1/threads/th-1/events"):
            events = [
                _ev("tool.started", "run-1", 1, call_id="c1", name="Terminal", args={}),
                _ev("message.delta", "run-1", 2, text="hi"),
                _ev("approval.required", "run-1", 3, approval_id="ap-1", args=CLI_ASK),
                _ev("run.failed", "run-1", 4, error_kind="bridge_lost"),
                _ev("run.finished", "run-2", 1),
            ]
            body = "".join(
                f"id: {i}\ndata: {json.dumps({**e, 'thread_seq': i})}\n\n"
                for i, e in enumerate(events, 1)
            )
            return httpx.Response(200, text=body)
        if m == "POST" and "/approvals/" in p:
            self.decided.append(p)
            return httpx.Response(409, json={"detail": "该确认已被处理过或已超时"})
        if (m, p) == ("GET", "/v1/runs/run-1"):
            return httpx.Response(
                200,
                json={"status": "failed", "error_kind": "bridge_lost",
                      "error_message": "会话 Pod 里的 bridge 重启了"},
            )  # fmt: skip
        if (m, p) == ("GET", "/v1/runs/run-2"):
            return httpx.Response(200, json={"status": "succeeded"})
        return httpx.Response(404, json={"detail": f"unexpected {m} {p}"})


async def test_expected_failures_are_not_errors_and_the_stream_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_POLL_S", 0.05)
    fake = _CliAtlas()
    plat = AtlasAgentPlatform(
        "http://atlas", BENCH_HEADERS, transport=httpx.MockTransport(fake.handler)
    )
    task = Task(
        id="cli-eng/x",
        suite="cli-eng",
        instruction="go",
        turns=[{"text": "again"}],
        run_outcomes={1: {"status": "failed", "error_kind": ["bridge_lost"]}},
    )
    try:
        tr = await plat.run_trial({"id": "ag"}, task, trial=1, approval_policy="delay:0.2")
        await asyncio.sleep(0)
    finally:
        await plat.aclose()
    assert tr.error is None and tr.error_kind is None  # 预期内的失败不记错误
    assert [(t["n"], t["status"], t["expected"]) for t in tr.turns] == [
        (1, "failed", True),
        (2, "succeeded", True),
    ]
    assert {"run-1", "run-2"} <= tr.stream_ended
    assert any(e["type"] == "message.delta" for e in tr.stream)  # 增量也留下了
    assert tr.turns[0]["first_event_s"] is not None


async def test_cli_approvals_record_the_command_and_kind_and_a_late_409_is_a_conflict() -> None:
    fake = _CliAtlas()
    plat = AtlasAgentPlatform(
        "http://atlas", BENCH_HEADERS, transport=httpx.MockTransport(fake.handler)
    )
    tr = TrialRun(thread_id="th-1")
    event = _ev("approval.required", "run-1", approval_id="ap-1", args=CLI_ASK)
    try:
        await plat._approve(tr, event, event["data"], "approved", 0.0)
    finally:
        await plat.aclose()
    [a] = tr.approvals
    assert a["command"] == "sh marks/mark.sh x 0" and a["kind"] == "execute"
    assert a["decision"] == "conflict"


def test_run_expect_matches_status_kind_and_message() -> None:
    e = RunExpect(status="failed", error_kind=["capability_unavailable"], message="模型")
    assert e.matches("failed", "capability_unavailable", "智能体没有分配模型 deepseek-flash")
    assert not e.matches("failed", "capability_unavailable", "MCP 不可用")
    assert not e.matches("failed", "internal_error", "模型")
    assert not e.matches("succeeded", None, None)
    assert RunExpect().matches("succeeded", None, None)


def test_a_declared_outcome_that_did_not_happen_is_not_a_pass() -> None:
    """预期第一轮被取消，实际因为模型不可用失败：没有产物的题校验天然通过，但这次不算做成。"""
    from atlas_bench.agent.runner import _declared_turn_missed

    task = Task(id="cli-eng/x", suite="cli-eng", instruction="x",
                run_outcomes={1: {"status": "cancelled"}})  # fmt: skip
    tr = TrialRun(thread_id="t")
    tr.turns = [{"n": 1, "status": "failed", "expected": False}]
    assert _declared_turn_missed(tr, task) == 1
    tr.turns = [{"n": 1, "status": "cancelled", "expected": True},
                {"n": 2, "status": "failed", "expected": False}]  # fmt: skip
    assert _declared_turn_missed(tr, task) is None  # 第 2 轮没声明：交给不变量 turns_unexpected


def test_workspace_changes_ignore_the_platform_area() -> None:
    initial = {"a.txt": "x", "img.png": b"\x89PNG"}
    after = {"a.txt": b"x", "img.png": b"\x89PNG", ".atlas/th/large_tool_results/1": b"..."}
    assert _workspace_changes(initial, after) == 0
    assert _workspace_changes(initial, {**after, "fib.py": b"print(1)"}) == 1
    assert _workspace_changes(initial, {"a.txt": b"y"}) == 2  # 改了一个、删了一个


# ─────────────────────────────────────────────── 会话 Pod 的集群操作


class _FakeCluster:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.argv: list[list[str]] = []

    async def __call__(self, argv: list[str], stdin: str | None, timeout: float) -> str:
        self.argv.append(argv)
        joined = " ".join(argv)
        for key, reply in self.replies.items():
            if key in joined:
                return reply
        return ""


async def test_cli_pods_are_found_in_their_namespace_and_deleted_gracefully_or_not() -> None:
    fake = _FakeCluster({"atlas-cli get pod cli-pod --ignore-not-found": "pod/cli-pod"})
    ops = ClusterOps(BenchSettings(), run=fake)
    await ops.delete_sandbox("cli-pod", graceful=True)
    assert fake.argv[-1] == ["kubectl", "-n", "atlas-cli", "delete", "pod", "cli-pod",
                             "--wait=false"]  # fmt: skip
    await ops.delete_sandbox("cli-pod")
    assert "--force" in fake.argv[-1] and "atlas-cli" in fake.argv[-1]


async def test_kill_cli_and_bridge_liveness_run_inside_the_session_pod() -> None:
    fake = _FakeCluster(
        {
            "atlas-cli get pod cli-pod --ignore-not-found": "pod/cli-pod",
            "python3": "42=node /opt/acp-cli/bin/claude-acp\n",
            "kill -0": "alive\n",
        }
    )
    ops = ClusterOps(BenchSettings(), run=fake)
    assert await ops.kill_cli("cli-pod", "th-1") == "42=node /opt/acp-cli/bin/claude-acp"
    argv = fake.argv[-1]
    assert argv[:6] == ["kubectl", "-n", "atlas-cli", "exec", "cli-pod", "-c"]
    assert argv[6] == "sandbox" and argv[-1] == "th-1"
    assert await ops.cli_session_alive("cli-pod", "th-1") is True
    assert fake.argv[-1][-1] == "th-1"
    gone = ClusterOps(BenchSettings(), run=_FakeCluster({}))
    assert await gone.cli_session_alive("cli-pod", "th-1") is False  # Pod 不在了


async def test_pods_with_image_matches_with_or_without_a_registry_prefix() -> None:
    def pod(name: str, image: str) -> dict[str, Any]:
        return {"metadata": {"name": name}, "spec": {"containers": [{"image": image}]}}

    items = [
        pod("a", "docker.io/library/atlas-cli-bridge:0.0.0-bench-missing"),
        pod("b", "atlas-cli-bridge:0.4.0"),
        pod("c", "atlas-cli-bridge:0.0.0-bench-missing"),
    ]
    fake = _FakeCluster({"get pods -o json": json.dumps({"items": items})})
    ops = ClusterOps(BenchSettings(), run=fake)
    assert await ops.pods_with_image("atlas-cli-bridge:0.0.0-bench-missing") == ["a", "c"]


async def test_stale_fault_policies_are_cleared_in_both_sandbox_namespaces() -> None:
    fake = _FakeCluster({"delete networkpolicy": "networkpolicy.networking.k8s.io/bench-block-1\n"})
    assert await ClusterOps(BenchSettings(), run=fake).clear_stale_faults() == 2
    assert {argv[2] for argv in fake.argv} == {"atlas-sbx", "atlas-cli"}


# ─────────────────────────────────────────────── 回收检查


class _ReclaimOps:
    def __init__(self, alive: set[tuple[str, str]], images: list[list[str]]) -> None:
        self.alive = alive
        self.images = images

    async def cli_session_alive(self, pod: str, session: str) -> bool:
        return (pod, session) in self.alive

    async def sandbox_exists(self, pod: str) -> bool:
        return False

    async def pods_with_image(self, image: str) -> list[str]:
        return self.images.pop(0) if len(self.images) > 1 else self.images[0]


class _Plat:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_thread(self, tr: TrialRun) -> None:
        self.deleted.append(tr.thread_id)


async def test_cli_reclaim_checks_the_session_bridge_not_the_shared_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import atlas_bench.agent.runner as runner

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    tr = TrialRun(thread_id="th-1")
    plat = _Plat()
    thread = Task(id="cli-eng/r", suite="cli-eng", instruction="x", reclaim_within_s=0)
    # 同一个 Pod 里还有别的会话（th-other），只看这个会话的 bridge
    ops = _ReclaimOps({("pod-a", "th-other")}, [[]])
    left = await _reclaim(plat, tr, ops, {"pod-a"}, thread, cli=True, runs=[])  # type: ignore[arg-type]
    assert left == 0 and plat.deleted == ["th-1"]

    # ephemeral 子会话：不删父会话，看子会话的 bridge
    subs = Task(id="cli-eng/s", suite="cli-eng", instruction="x", reclaim_within_s=0,
                reclaim_scope="subagents")  # fmt: skip
    runs = [{"thread_id": "th-1"}, {"thread_id": "th-sub", "parent_run_id": "r1"}]
    ops = _ReclaimOps({("pod-a", "th-sub")}, [[]])
    plat = _Plat()
    left = await _reclaim(plat, tr, ops, {"pod-a"}, subs, cli=True, runs=runs)  # type: ignore[arg-type]
    assert left == 1 and plat.deleted == []

    # 起不来的镜像：用这个镜像的 Pod 都没了才算
    image = Task(id="cli-eng/i", suite="cli-eng", instruction="x", reclaim_within_s=0,
                 reclaim_scope="image", agent_patch={"cli": {"image": "img:missing"}})  # fmt: skip
    ops = _ReclaimOps(set(), [["pending-pod"]])
    assert await _reclaim(_Plat(), tr, ops, set(), image, cli=True, runs=[]) == 1  # type: ignore[arg-type]


# ─────────────────────────────────────────────── 套件共享的断言


def test_shared_expectations_are_added_and_a_task_can_override_one(tmp_path: Path) -> None:
    import atlas_bench.agent.tasks as tasks_mod

    suite = tmp_path / "s"
    (suite / "_shared").mkdir(parents=True)
    (suite / "_shared" / "expect.yaml").write_text(
        "- {desc: 没有 session.lost, event: session.lost, count: '==0'}\n"
        "- {desc: 正文一致, metric: answer_mismatch, count: '==0'}\n",
        encoding="utf-8",
    )
    (suite / "t").mkdir()
    (suite / "t" / "task.yaml").write_text(
        "instruction: x\nexpect:\n  - {desc: 恰好一次, event: session.lost, count: '==1'}\n",
        encoding="utf-8",
    )
    old = tasks_mod.TASK_DIRS
    tasks_mod.TASK_DIRS = [tmp_path]
    try:
        [task] = load_suite("s")
    finally:
        tasks_mod.TASK_DIRS = old
    assert [(x.desc, x.count) for x in task.expect] == [("正文一致", "==0"), ("恰好一次", "==1")]


def test_cli_suite_shared_invariants_reach_every_task() -> None:
    tasks = load_suite("cli-eng")
    assert len(tasks) == 35
    for t in tasks:
        keys = {x.key() for x in t.expect}
        assert {"metric:answer_mismatch", "metric:tool_terminal_dup", "metric:turns_unexpected",
                "event:session.lost"} <= keys, t.id  # fmt: skip
        for action in t.actions:  # 触发条件都真的解析出来了（when 不是 on）
            assert action.when is not None, t.id
        if t.reclaim_scope == "image":
            assert (t.agent_patch.get("cli") or {}).get("image"), t.id
