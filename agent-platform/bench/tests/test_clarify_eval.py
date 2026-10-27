"""需求澄清评测：模拟用户、提问的处理、指标与报告、任务集自检。

设计：doc/ask-user-question-design.html §12。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_bench.agent.platforms.atlas import AtlasAgentPlatform
from atlas_bench.agent.report import write_agent_report
from atlas_bench.agent.schema import AgentExperimentConfig, Task, TrialResult, VariantConfig
from atlas_bench.agent.tasks import load_suite
from atlas_bench.agent.trajectory import metrics_from_trace
from atlas_bench.agent.user import normalize_answer

BENCH_HEADERS = {"X-Uid": "00000000-0000-0000-0000-0000000000be", "X-Caller-App": "bench"}

QUESTIONS = [
    {
        "question": "做哪类工作流？",
        "header": "业务场景",
        "options": [
            {"label": "审批流", "recommended": True},
            {"label": "工单流转"},
        ],
    },
    {
        "question": "需要哪些能力？",
        "header": "关键能力",
        "multi_select": True,
        "options": [{"label": "转派"}, {"label": "催办"}, {"label": "并行"}],
    },
]


# ─────────────────────────────────────────────── 任务集


def _points(task: Task) -> list[str]:
    """校验脚本里的要点（POINTS）—— 从脚本源码取，避免两处各写一份。"""
    tree = ast.parse((Path(task.dir) / "verifier" / "verify.py").read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "POINTS"
    )
    return ast.literal_eval(node.value)


def test_the_clarify_suite_comes_in_pairs() -> None:
    """每个需求一对：模糊题的指令里没有要点（不问就不知道），清晰题写明了；两题都带模拟用户。"""
    tasks = {t.id.split("/")[1]: t for t in load_suite("clarify")}
    assert sorted(tasks) == sorted(
        f"{n}-{k}" for n in ("workflow", "dashboard", "collab") for k in ("vague", "clear")
    )
    for name in ("workflow", "dashboard", "collab"):
        vague, clear = tasks[f"{name}-vague"], tasks[f"{name}-clear"]
        assert "ambiguous" in vague.tags and "clear" in clear.tags
        assert vague.user and clear.user and vague.user.brief == clear.user.brief
        points = _points(vague)
        assert points == _points(clear)
        assert [p for p in points if p in vague.instruction] == []
        assert [p for p in points if p not in clear.instruction] == []
        assert [p for p in points if p not in vague.user.brief] == [], "模拟用户得知道这些要点"


# ─────────────────────────────────────────────── 模拟用户


def test_answers_are_shaped_for_the_server() -> None:
    single, multi = QUESTIONS
    # 不存在的选项丢掉；单选只留一个，选了选项就不留「其他」
    assert normalize_answer(single, {"selected": ["工单流转", "审批流", "BPMN"], "other": "x"}) == {
        "selected": ["工单流转"],
        "other": None,
    }
    assert normalize_answer(single, {"selected": [], "other": " 我要工单 "}) == {
        "selected": [],
        "other": "我要工单",
    }
    # 多选：选项与「其他」可以同时有；重复的去掉
    assert normalize_answer(multi, {"selected": ["转派", "转派", "催办"], "other": "要能回退"}) == {
        "selected": ["转派", "催办"],
        "other": "要能回退",
    }
    assert normalize_answer(multi, {}) == {"selected": [], "other": None}


class _StubUser:
    def __init__(self, answers: list[dict[str, Any]] | None = None) -> None:
        self.answers = answers
        self.asked: list[tuple[str, list[dict[str, Any]]]] = []

    async def answer(self, brief: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.asked.append((brief, questions))
        if self.answers is None:
            raise RuntimeError("模拟用户的模型不可用")
        return self.answers


class _FakeAtlas:
    """会话流里只有一个问题（approval_kind=question）的假 atlas-server。"""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        if (m, p) == ("POST", "/v1/threads"):
            return httpx.Response(201, json={"id": "th-1"})
        if (m, p) == ("POST", "/v1/threads/th-1/runs"):
            return httpx.Response(202, json={"run_id": "run-1"})
        if (m, p) == ("GET", "/v1/threads/th-1/events"):
            ev = {
                "type": "approval.required",
                "run_id": "run-1",
                "data": {
                    "approval_id": "q-1",
                    "tool_name": "ask_user_question",
                    "approval_kind": "question",
                    "args": {"questions": QUESTIONS},
                },
            }
            return httpx.Response(200, text=f"id: 3\ndata: {json.dumps(ev)}\n\n")
        if (m, p) == ("POST", "/v1/runs/run-1/approvals/q-1"):
            self.decisions.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if (m, p) == ("GET", "/v1/runs/run-1"):
            # 等问题答完才结束，免得监听还没处理完事件 run 就收尾了
            status = "succeeded" if self.decisions else "running"
            return httpx.Response(200, json={"status": status})
        return httpx.Response(404, json={"detail": f"unexpected {m} {p}"})


async def _trial(task: Task, answerer: Any, monkeypatch: pytest.MonkeyPatch):
    import atlas_bench.agent.platforms.atlas as mod

    monkeypatch.setattr(mod, "_POLL_S", 0.05)
    fake = _FakeAtlas()
    plat = AtlasAgentPlatform(
        "http://atlas", BENCH_HEADERS, transport=httpx.MockTransport(fake.handler)
    )
    try:
        tr = await plat.run_trial(
            {"id": "ag-1"}, task, trial=1, approval_policy="deny_all", answerer=answerer
        )
    finally:
        await plat.aclose()
    return tr, fake


async def test_the_simulated_user_answers_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 问题由模拟用户按 brief 作答；不受审批策略管（这里策略是 deny_all），也不记成审批。"""
    answers = [{"selected": ["工单流转"], "other": None}, {"selected": ["转派"], "other": None}]
    user = _StubUser(answers)
    task = Task(id="clarify/x", suite="clarify", instruction="x", user={"brief": "要工单"})
    tr, fake = await _trial(task, user, monkeypatch)
    assert user.asked == [("要工单", QUESTIONS)]
    assert fake.decisions == [{"decision": "approved", "answers": answers}]
    assert tr.approvals == []
    assert [(q["by"], q["n"], q["headers"]) for q in tr.questions] == [
        ("simulated", 2, ["业务场景", "关键能力"])
    ]


async def test_without_a_simulated_user_questions_take_the_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务没带模拟用户：不带答案批准 —— 服务端按推荐项回答（与原来的行为一致）。"""
    task = Task(id="demo/x", suite="demo", instruction="x")
    tr, fake = await _trial(task, _StubUser([]), monkeypatch)
    assert fake.decisions == [{"decision": "approved"}]
    assert tr.questions[0]["by"] == "recommended"


async def test_a_failing_simulated_user_does_not_stall_the_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟用户出错：退回按推荐并记下错误 —— 不能让 run 一直挂着等回答。"""
    task = Task(id="clarify/x", suite="clarify", instruction="x", user={"brief": "要工单"})
    tr, fake = await _trial(task, _StubUser(None), monkeypatch)
    assert fake.decisions == [{"decision": "approved"}]
    assert tr.questions[0]["by"] == "recommended"
    assert tr.questions[0]["error"] == "模拟用户的模型不可用"


def test_a_simulated_user_failure_voids_the_trial() -> None:
    """模拟用户出错（问题已按推荐回答）：这次不是要测的对话。

    记评测侧错误，不进分母、续跑时重做。
    """
    from atlas_bench.agent.platforms.atlas import TrialRun
    from atlas_bench.agent.runner import _classify

    tr = TrialRun(thread_id="t", status="succeeded")
    assert _classify(tr, True, {"simulated_user_errors": 1}) == "infra_error"
    assert _classify(tr, True, {"simulated_user_errors": 0}) == "success"


async def test_user_tasks_need_a_simulated_user() -> None:
    """选中的任务带模拟用户、实验却没配 user_model：早报，别让问题悄悄按推荐回答。"""
    from atlas_bench.agent.runner import run_agent_experiment
    from atlas_bench.settings import BenchSettings

    cfg = AgentExperimentConfig(
        name="c", suites=["clarify"], variants={"a": VariantConfig(agent={})}
    )
    with pytest.raises(SystemExit):
        await run_agent_experiment(cfg, BenchSettings(), platform=None)  # type: ignore[arg-type]


# ─────────────────────────────────────────────── 指标与报告


def test_questions_are_counted_apart_from_approvals() -> None:
    ev = [
        {
            "type": "approval.required",
            "data": {"approval_kind": "question", "args": {"questions": QUESTIONS}},
        },
        {"type": "approval.required", "data": {"approval_kind": "tool", "tool_name": "execute"}},
        # 旧事件没有 approval_kind：按审批算
        {"type": "approval.required", "data": {"tool_name": "execute"}},
    ]
    m = metrics_from_trace(ev, [])
    assert (m["question_rounds"], m["questions_asked"], m["approvals"]) == (1, 2, 2)


def test_the_report_summarizes_asking_on_vague_and_clear_tasks(tmp_path: Path) -> None:
    cfg = AgentExperimentConfig(
        name="clarify", suites=["clarify"], variants={"ask": VariantConfig(agent={})}
    )
    tasks = [
        {"id": "clarify/w-vague", "category": "dialog", "tags": ["clarify", "ambiguous"]},
        {"id": "clarify/w-clear", "category": "dialog", "tags": ["clarify", "clear"]},
    ]
    manifest = {
        "config": cfg.model_dump(mode="json"),
        "git_commit": "abc",
        "started_at": "now",
        "tasks": tasks,
        "agents": {"ask": {"slug": "bench-ask"}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    rows = [
        # 模糊题：两次里问了一次（问的那次做对了）
        ("clarify/w-vague", 1, True, {"question_rounds": 1, "questions_asked": 3}),
        ("clarify/w-vague", 2, False, {"question_rounds": 0}),
        # 清晰题：都没问、都做对
        ("clarify/w-clear", 1, True, {"question_rounds": 0}),
        ("clarify/w-clear", 2, True, {"question_rounds": 0, "simulated_user_errors": 0}),
    ]
    (tmp_path / "trials").mkdir()
    (tmp_path / "trials" / "ask.jsonl").write_text(
        "\n".join(
            TrialResult(
                task_id=t, variant="ask", trial=n, outcome="success" if ok else "failure",
                passed=ok, metrics=metrics,
            ).model_dump_json()
            for t, n, ok, metrics in rows
        )
        + "\n"
    )  # fmt: skip
    write_agent_report(tmp_path)
    clarify = json.loads((tmp_path / "summary.json").read_text())["variants"]["ask"]["clarify"]
    assert clarify == {
        "ambiguous_trials": 2,
        "ask_rate": 0.5,
        "ambiguous_success": 0.5,
        "clear_trials": 2,
        "over_ask_rate": 0.0,
        "clear_success": 1.0,
        "questions_per_round": 3.0,
        "simulated_user_errors": 0,
    }
