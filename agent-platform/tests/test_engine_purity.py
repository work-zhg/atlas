"""★ G2 守卫：engine 必须是纯库，能脱离 DB / HTTP 框架单测。

文档 §16 P1 的完成标准：如果这些测试需要起 Postgres，说明分层已经破了。
import-linter 在静态层面挡（pyproject [tool.importlinter]），这里在运行时兜一层。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from atlas_engine.contracts import InvalidSpec
from atlas_engine.contracts.events import TERMINAL_EVENTS, EventType, TraceEvent
from atlas_engine.contracts.spec import AgentSpec, CompactionSpec, ModelSpec, SubAgentSpec

FORBIDDEN = ("sqlalchemy", "fastapi", "redis", "alembic", "uvicorn", "pydantic_settings")


def test_engine_import_pulls_no_server_dependency() -> None:
    """在干净子进程里 import engine，确认没有 DB / web 框架被拉进来。"""
    modules = ("contracts", "kernel", "runtime")
    code = (
        "import sys, importlib;"
        f"[importlib.import_module('atlas_engine.' + m) for m in {modules!r}];"
        f"bad=[m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = proc.stdout.strip()
    assert leaked == "", f"engine 泄漏了 server 侧依赖：{leaked}"


# ---------------------------------------------------------------------------
# 模型参数：意图原样发，模型收不收由模型网关按登记的能力整理（不按模型名猜）
# ---------------------------------------------------------------------------


def test_model_intent_is_not_judged_by_model_name() -> None:
    """engine 不按模型名判断能收什么参数：温度、关思考、档位都原样放过。"""
    ModelSpec(model="claude-opus-5", temperature=0.2).validate()
    ModelSpec(model="claude-fable-5", thinking="off", effort="max").validate()
    ModelSpec(model="claude-haiku-4-5", thinking="adaptive", effort="low").validate()


def test_auto_intent_is_adaptive_thinking_at_high_effort() -> None:
    spec = ModelSpec(model="any-model")
    assert spec.resolve_thinking() == "adaptive"
    assert spec.resolve_effort() == "high"


def test_explicit_intent_is_kept() -> None:
    spec = ModelSpec(model="any-model", thinking="off", effort="low")
    assert spec.resolve_thinking() == "off"
    assert spec.resolve_effort() == "low"


def test_max_output_tokens_must_be_positive() -> None:
    with pytest.raises(InvalidSpec, match="max_output_tokens"):
        ModelSpec(model="m", max_output_tokens=0).validate()


# ---------------------------------------------------------------------------
# CompactionSpec —— target_ratio 必须严格小于 trigger_ratio（§7.3 坑 2）
# ---------------------------------------------------------------------------


def test_compaction_rejects_target_above_trigger() -> None:
    with pytest.raises(InvalidSpec, match="target_ratio"):
        CompactionSpec(trigger_ratio=0.5, target_ratio=0.8).validate()


def test_compaction_defaults_valid() -> None:
    CompactionSpec().validate()


# ---------------------------------------------------------------------------
# AgentSpec
# ---------------------------------------------------------------------------


def _sub(name: str) -> SubAgentSpec:
    return SubAgentSpec(
        name=name,
        description="d",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
    )


def test_agent_spec_rejects_duplicate_subagent_names() -> None:
    spec = AgentSpec(
        slug="a",
        name="A",
        system_prompt="p",
        model=ModelSpec(model="claude-opus-5"),
        subagents=(_sub("research"), _sub("research")),
    )
    with pytest.raises(InvalidSpec, match="唯一"):
        spec.validate()


def test_agent_spec_propagates_subagent_model_validation() -> None:
    bad = SubAgentSpec(
        name="x",
        description="d",
        system_prompt="p",
        model=ModelSpec(model="claude-opus-5", max_output_tokens=0),
    )
    spec = AgentSpec(
        slug="a",
        name="A",
        system_prompt="p",
        model=ModelSpec(model="claude-opus-5"),
        subagents=(bad,),
    )
    with pytest.raises(InvalidSpec, match="max_output_tokens"):
        spec.validate()


# ---------------------------------------------------------------------------
# TraceEvent 契约
# ---------------------------------------------------------------------------


def _event(seq: int, type_: EventType) -> TraceEvent:
    from datetime import UTC, datetime
    from uuid import UUID

    return TraceEvent(
        seq=seq,
        run_id=UUID("11111111-1111-1111-1111-111111111111"),
        ts=datetime(2026, 8, 19, tzinfo=UTC),
        type=type_,
    )


def test_seq_must_start_at_one() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _event(0, EventType.RUN_STARTED)


def test_terminal_events_are_marked() -> None:
    assert _event(1, EventType.RUN_FINISHED).is_terminal
    assert _event(1, EventType.RUN_FAILED).is_terminal
    assert _event(1, EventType.RUN_CANCELLED).is_terminal
    assert not _event(1, EventType.MESSAGE_DELTA).is_terminal
    assert len(TERMINAL_EVENTS) == 3


def test_sse_frame_uses_seq_as_id() -> None:
    """浏览器 EventSource 断线重连靠 id 带 Last-Event-ID（§10.2）。"""
    frame = _event(42, EventType.MESSAGE_DELTA).to_sse()
    assert frame.startswith("id: 42\n")
    assert "event: message.delta\n" in frame
    assert frame.endswith("\n\n")


def test_todos_updated_is_a_snapshot_contract() -> None:
    """契约规则 1：全量快照。此测试是文档化断言，防止有人改成增量。"""
    assert EventType.TODOS_UPDATED.value == "todos.updated"


# ---------------------------------------------------------------- kernel 边界
#
# 历史注：这里曾有「server 禁 import kernel」与「外层只许 agent.py 碰 kernel」
# 两条守卫 —— 前提都是 kernel 可替换、agent.py 是防腐层。kernel 永久内化、
# server 直接装配之后两条一起撤销；留下的这条方向纪律反而更关键了：
# 全部中间件都住进了 kernel，它反向摸 spec / runner 的诱惑更大。


def test_kernel_imports_from_the_host_only_via_contracts() -> None:
    """kernel 对宿主的 import **只许指向 atlas_engine.contracts**。

    协议定义上移之后这条边是刻意反转的：kernel 依赖契约层，但不许依赖
    spec / hooks / runner 这些外层模块 —— 否则「engine 装配」与「kernel
    执行」会缠成一个环，加能力就又要动两层。contracts 是唯一的向上通道。
    """
    import re
    from pathlib import Path

    kernel = Path(__file__).resolve().parents[1] / "libs" / "engine" / "src" / "atlas_engine" / "kernel"
    pattern = re.compile(r"^\s*(?:from|import)\s+(atlas_engine[.\w]*)", re.M)
    offenders: list[str] = []
    for f in kernel.rglob("*.py"):
        for target in pattern.findall(f.read_text(encoding="utf-8")):
            if not target.startswith(("atlas_engine.kernel", "atlas_engine.contracts")):
                offenders.append(f"{f.name} → {target}")
    assert offenders == [], f"kernel 越过 contracts 依赖了外层：{offenders}"


def test_cli_translation_pulls_no_infrastructure() -> None:
    """server 里留下的纯计算层（CLI 运行时的 ACP 翻译）不许拖进基础设施。

    原生运行时的词汇、装配、执行循环已搬进 engine（test_engine_import_pulls_no_server_dependency
    兜住）；这里只剩 server 自己的那一块。import-linter 有同款 contract（CI）。
    """
    modules = ("atlas_agent.cli.translate.acp_v1",)
    code = (
        "import sys, importlib;"
        f"[importlib.import_module(m) for m in {modules!r}];"
        f"bad=[m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = proc.stdout.strip()
    assert leaked == "", f"server 纯计算层拖进了基础设施：{leaked}"
