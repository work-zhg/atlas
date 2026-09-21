"""★ G2 守卫：engine 必须是纯库，能脱离 DB / HTTP 框架单测。

文档 §16 P1 的完成标准：如果这些测试需要起 Postgres，说明分层已经破了。
import-linter 在静态层面挡（pyproject [tool.importlinter]），这里在运行时兜一层。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from atlas_engine.contracts import InvalidSpec
from atlas_server.domain.events import TERMINAL_EVENTS, EventType, TraceEvent
from atlas_server.domain.spec import AgentSpec, CompactionSpec, ModelSpec, SubAgentSpec

FORBIDDEN = ("sqlalchemy", "fastapi", "redis", "alembic", "uvicorn", "pydantic_settings")


def test_engine_import_pulls_no_server_dependency() -> None:
    """在干净子进程里 import engine，确认没有 DB / web 框架被拉进来。"""
    modules = ("contracts", "kernel")
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
# 把 2026-08-19 网关实测结论锁进测试（docs/backend-design.md §3）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-fable-5"])
def test_temperature_rejected_on_models_that_400(model: str) -> None:
    """实测：这些模型返回 `temperature` is deprecated for this model. (HTTP 400)"""
    spec = ModelSpec(model=model, temperature=0.2)
    with pytest.raises(InvalidSpec, match="temperature"):
        spec.validate()


def test_temperature_allowed_on_haiku() -> None:
    """haiku-4-5 是唯一接受 temperature 的模型。"""
    ModelSpec(model="claude-haiku-4-5", temperature=0.7).validate()


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_thinking_off_conflicts_with_high_effort(effort: str) -> None:
    """实测/文档：opus-5 上 thinking=disabled 仅 effort <= high 合法。"""
    with pytest.raises(InvalidSpec, match="thinking"):
        ModelSpec(model="claude-opus-5", thinking="off", effort=effort).validate()  # type: ignore[arg-type]


def test_thinking_off_ok_at_high_effort() -> None:
    ModelSpec(model="claude-opus-5", thinking="off", effort="high").validate()


# ---------------------------------------------------------------------------
# adaptive thinking 的模型能力门禁
# 实测：haiku-4-5 传 adaptive → 400 "adaptive thinking is not supported on this model"
# 它同时是摘要/标题生成的默认模型，错了会在 P4/P5 才炸。
# ---------------------------------------------------------------------------


def test_adaptive_thinking_rejected_on_haiku() -> None:
    with pytest.raises(InvalidSpec, match="adaptive"):
        ModelSpec(model="claude-haiku-4-5", thinking="adaptive").validate()


def test_auto_resolves_to_none_on_haiku() -> None:
    """auto 在不支持的模型上解析为 none —— 即完全不发送 thinking 参数。"""
    spec = ModelSpec(model="claude-haiku-4-5")
    spec.validate()
    assert spec.thinking == "auto"
    assert spec.resolve_thinking() == "none"


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-fable-5"])
def test_auto_resolves_to_adaptive_on_46plus(model: str) -> None:
    assert ModelSpec(model=model).resolve_thinking() == "adaptive"


def test_thinking_cannot_be_disabled_on_fable() -> None:
    """fable-5 思考恒开，显式 disabled 会 400。"""
    with pytest.raises(InvalidSpec, match="无法关闭"):
        ModelSpec(model="claude-fable-5", thinking="off", effort="high").validate()


# ---------------------------------------------------------------------------
# effort 的模型能力门禁
# 实测：haiku-4-5 传 output_config.effort → 400
#   "This model does not support the effort parameter."
# ---------------------------------------------------------------------------


def test_effort_rejected_on_haiku() -> None:
    with pytest.raises(InvalidSpec, match="effort"):
        ModelSpec(model="claude-haiku-4-5", effort="low").validate()


def test_effort_auto_resolves_to_none_on_haiku() -> None:
    spec = ModelSpec(model="claude-haiku-4-5")
    spec.validate()
    assert spec.resolve_effort() is None


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-fable-5"])
def test_effort_auto_defaults_to_high(model: str) -> None:
    assert ModelSpec(model=model).resolve_effort() == "high"


def test_summarizer_default_spec_is_valid() -> None:
    """haiku-4-5 是 §7 摘要与 §8 标题生成的默认模型 —— 开箱即用必须合法。

    这条曾经是坏的：默认 spec 同时带上 adaptive thinking 和 effort，
    两者 haiku 都不收，会在 P4/P5 才炸。
    """
    spec = ModelSpec(model="claude-haiku-4-5")
    spec.validate()
    assert spec.resolve_effort() is None
    assert spec.resolve_thinking() == "none"


def test_default_model_spec_is_valid() -> None:
    ModelSpec(model="claude-opus-5").validate()


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
        model=ModelSpec(model="claude-opus-5", temperature=0.5),
    )
    spec = AgentSpec(
        slug="a",
        name="A",
        system_prompt="p",
        model=ModelSpec(model="claude-opus-5"),
        subagents=(bad,),
    )
    with pytest.raises(InvalidSpec):
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

    kernel = Path(__file__).resolve().parents[1] / "engine" / "src" / "atlas_engine" / "kernel"
    pattern = re.compile(r"^\s*(?:from|import)\s+(atlas_engine[.\w]*)", re.M)
    offenders: list[str] = []
    for f in kernel.rglob("*.py"):
        for target in pattern.findall(f.read_text(encoding="utf-8")):
            if not target.startswith(("atlas_engine.kernel", "atlas_engine.contracts")):
                offenders.append(f"{f.name} → {target}")
    assert offenders == [], f"kernel 越过 contracts 依赖了外层：{offenders}"


def test_server_pure_layer_pulls_no_infrastructure() -> None:
    """server 里的纯计算层（词汇 / 装配 / 执行循环）不许拖进基础设施。

    外层搬进 server 之后，「run 一轮可以脱离 DB/Redis 测试」这条性质不再由
    包边界天然保证 —— 有人在 runner 里顺手 import 一个 repository，性质就
    静默消失了。import-linter 有同款 contract（CI），这里让本地套件也兜住。
    """
    modules = (
        "atlas_server.domain.spec",
        "atlas_server.domain.events",
        "atlas_server.domain.translator",
        "atlas_server.domain.tool_registry",
        "atlas_server.executor.runner",
        "atlas_server.executor.build",
        "atlas_server.acp.translate",
    )
    code = (
        "import sys, importlib;"
        f"[importlib.import_module(m) for m in {modules!r}];"
        f"bad=[m for m in {FORBIDDEN!r} if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    leaked = proc.stdout.strip()
    assert leaked == "", f"server 纯计算层拖进了基础设施：{leaked}"
