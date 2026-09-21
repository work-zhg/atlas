"""遥测：事件流 → span 树（可观测性设计 §04）。

全部脱离 Collector 与网络 —— 用 InMemorySpanExporter 收 span，断言的是
真实的派生逻辑，不是 mock 出来的行为。

## 为什么这组测试的重点是「关掉时什么都不做」

遥测是旁路。它出问题的方式不是报错，而是**悄悄改变主路径的行为** ——
多一个异常、多一次阻塞、多占一段内存。所以第一条断言不是「span 对不对」，
而是「关掉开关时它完全不存在」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from atlas_server.domain.events import EventFactory, EventType, TraceEvent
from atlas_server.domain.spec import AgentSpec, CliSpec, ModelSpec
from atlas_server.telemetry import semconv as sc
from atlas_server.telemetry.run_trace import RunTrace

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")
THREAD_ID = UUID("88888888-8888-8888-8888-888888888888")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def spans():
    """装一个只进内存的 TracerProvider，返回取 span 的函数。

    ★ 不碰全局 provider 的复原：opentelemetry 只允许设置一次全局 provider，
      重复设置会打警告并忽略。所以这里用**自己的** tracer provider，通过
      monkeypatch 把 telemetry.tracer 指过去 —— 测试之间互不影响。
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture
def traced(spans, monkeypatch):
    provider, exporter = spans
    import atlas_server.telemetry as telemetry
    import atlas_server.telemetry.run_trace as run_trace_mod

    monkeypatch.setattr(telemetry, "tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(run_trace_mod, "tracer", lambda: provider.get_tracer("test"), raising=False)
    return exporter


def make_spec(kind: str = "native") -> AgentSpec:
    return AgentSpec(
        slug="analyst",
        name="分析师",
        system_prompt="",
        model=ModelSpec(model="claude-sonnet-5"),
        kind=kind,  # type: ignore[arg-type]
        cli=CliSpec(cli_type="claude-code") if kind == "acp" else None,
    )


class _Prepared:
    """PreparedRun 的最小替身 —— RunTrace 只读 spec 与 thread_id。"""

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.thread_id = THREAD_ID


def events_of(*pairs: tuple[EventType, dict], depth: int = 0) -> list[TraceEvent]:
    """按秒递增造一串事件 —— span 时间取自事件的 ts，所以必须有区分度。"""
    factory = EventFactory(RUN_ID, lambda: _T0)
    out: list[TraceEvent] = []
    for i, (kind, data) in enumerate(pairs):
        event = factory.make(kind, data, depth)
        out.append(event.model_copy(update={"ts": _T0 + timedelta(seconds=i)}))
    return out


def by_name(exporter) -> dict[str, object]:
    return {s.name: s for s in exporter.get_finished_spans()}


# ──────────────────────────────────────────────── 关掉时零副作用


def test_disabled_setup_installs_nothing() -> None:
    """★ 最重要的一条：没启用时 setup 不装任何东西、也不抛。

    opentelemetry-api 在没有 SDK 时是 no-op，所以埋点处才敢不写 if。
    这条要是破了，「关着的时候零开销」这个前提就没了。
    """
    from atlas_server.config import Settings
    from atlas_server.telemetry import setup_telemetry, shutdown_telemetry

    settings = Settings(
        litellm_key="x", default_user_id=RUN_ID, database_url="x", redis_url="x"
    )
    assert settings.otel_enabled is False

    setup_telemetry(settings)  # 不抛
    import atlas_server.telemetry as telemetry

    assert telemetry._provider is None
    shutdown_telemetry()  # 也不抛


def test_run_trace_survives_a_broken_event(traced) -> None:
    """事件形状不对时只吞掉那一条，不让 run 跟着失败。"""
    rt = RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID))
    # ts 不是 datetime → _ns() 里 .timestamp() 直接 AttributeError。
    # ★ 要挑一个**真的会抛**的畸形：早先这里塞的是 call_id=object()，
    #   而 str(object()) 根本不报错 —— 那条断言等于没测。
    bogus = events_of((EventType.TOOL_STARTED, {"call_id": "c1", "name": "x"}))[0]
    bogus = bogus.model_copy(update={"ts": "不是时间"})
    rt.observe(bogus)  # 不抛
    rt.close()
    assert "invoke_agent analyst" in by_name(traced)


# ──────────────────────────────────────────────── span 树


def test_run_is_the_root_with_genai_attributes(traced) -> None:
    """Run 是 trace 的根 —— 不是 Session（太长），也不是单次模型调用（太碎）。"""
    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        rt.observe(events_of((EventType.RUN_STARTED, {}))[0])

    root = by_name(traced)["invoke_agent analyst"]
    attrs = root.attributes
    assert attrs[sc.OPERATION_NAME] == sc.OP_INVOKE_AGENT
    # §06：这几个维度正是会话管理里已有的主键
    assert attrs[sc.CONVERSATION_ID] == str(THREAD_ID)
    assert attrs[sc.RUN_ID] == str(RUN_ID)
    assert attrs[sc.AGENT_KIND] == "native"


def test_spans_started_inside_the_run_nest_under_it(traced) -> None:
    """★ 根 span 必须是**当前 span**，否则模型与审批 span 会变成孤儿。

    这两类 span 靠「当前上下文里有谁」找父：模型调用在 LangChain 回调里
    起 span，审批等待在 gate 里起 span，两处都够不到 RunTrace 的实例。
    早先 RunTrace 只 start_span 而没设 current —— 工具 span 挂对了（它显式
    传 context），chat span 却全是平行的根。Jaeger 上看就是一堆与 Run 没有
    任何关系的 chat，而那正好废掉了 trace 的意义。端到端跑一次才发现的。
    """
    import atlas_server.telemetry as telemetry

    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)):  # noqa: SIM117
        # ★ 嵌套是这条测试的**全部意义**：外层不把 span 设成 current 的话，
        #   内层就找不到父。合并成一个 with 会把被测的关系消掉。
        # 模仿模型回调 / 审批 gate：它们不知道 RunTrace 的存在，只是从
        # telemetry.tracer() 取 tracer 并 start_as_current_span —— 父靠当前上下文
        with telemetry.tracer().start_as_current_span("chat deepseek-chat"):
            pass

    spans = by_name(traced)
    assert spans["chat deepseek-chat"].parent is not None, "chat span 成了孤儿"
    assert (
        spans["chat deepseek-chat"].parent.span_id
        == spans["invoke_agent analyst"].context.span_id
    )


def test_tool_calls_become_child_spans(traced) -> None:
    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "read_file"}),
            (EventType.TOOL_COMPLETED, {"call_id": "c1", "status": "success"}),
        ):
            rt.observe(e)

    tool = by_name(traced)["execute_tool read_file"]
    assert tool.attributes[sc.OPERATION_NAME] == sc.OP_EXECUTE_TOOL
    assert tool.attributes[sc.TOOL_NAME] == "read_file"
    # 子挂在根下
    root = by_name(traced)["invoke_agent analyst"]
    assert tool.parent.span_id == root.context.span_id
    # 时间取自事件自带的 ts，不是「现在」
    assert tool.end_time - tool.start_time == 1_000_000_000


def test_duplicate_tool_started_makes_one_span(traced) -> None:
    """★ acp 的一次工具调用会发**两条** tool.started。

    真 CLI 上实测：`tool_call` 与随后的 `tool_call_update` 各一条，
    两条的 call_id 相同。不去重的话，Jaeger 上每次工具调用都会出现一个
    永远不结束的影子 span。
    """
    with RunTrace.start(_Prepared(make_spec("acp")), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Write"}),
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Write /workspace/a.md"}),
            (EventType.TOOL_COMPLETED, {"call_id": "c1", "status": "success"}),
        ):
            rt.observe(e)

    tools = [s for s in traced.get_finished_spans() if s.name.startswith("execute_tool")]
    assert len(tools) == 1, [s.name for s in tools]


def test_unfinished_tool_is_closed_as_error(traced) -> None:
    """run 失败时 tool.started 等不到完成事件 —— 不兜底就永远挂着。"""
    from opentelemetry.trace import StatusCode

    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "bash"}),
            (EventType.RUN_FAILED, {"error_kind": "model_unavailable", "message": "网关挂了"}),
        ):
            rt.observe(e)

    spans = by_name(traced)
    assert spans["execute_tool bash"].status.status_code is StatusCode.ERROR
    root = spans["invoke_agent analyst"]
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes["error.type"] == "model_unavailable"


def test_subagent_delegation_is_a_child_span(traced) -> None:
    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.SUBAGENT_STARTED, {"subagent_run_id": "s1", "name": "coder", "task": "写"}),
            (EventType.SUBAGENT_FINISHED, {"subagent_run_id": "s1", "status": "success"}),
        ):
            rt.observe(e)

    span = by_name(traced)["invoke_agent coder"]
    assert span.attributes[sc.SUBAGENT_NAME] == "coder"


def test_subagent_internal_steps_stay_out_of_the_parent_trace(traced) -> None:
    """★ depth>0 是子智能体的内部步骤，它有自己的 run 和自己的 trace。

    挂进父 trace 等于把子任务的中间过程搬回主链路 —— 正是委派设计要避免
    的那件事（父只拿回结论）。
    """
    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "inner", "name": "grep"}), depth=1
        ):
            rt.observe(e)

    assert not [s for s in traced.get_finished_spans() if s.name.startswith("execute_tool")]


def test_usage_lands_on_the_root_with_cache_split_out(traced) -> None:
    """★ 缓存与推理 token 单独记，不并进 input/output。

    它们计价不同，合并会让成本核算系统性偏离，长会话尤其明显（§03）。
    """
    with RunTrace.start(_Prepared(make_spec()), run_id=str(RUN_ID)) as rt:
        rt.observe(
            events_of(
                (
                    EventType.USAGE_UPDATED,
                    {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read": 900,
                        "cache_creation": 50,
                        "thinking_tokens": 7,
                    },
                )
            )[0]
        )

    attrs = by_name(traced)["invoke_agent analyst"].attributes
    assert attrs[sc.USAGE_INPUT] == 100
    assert attrs[sc.USAGE_OUTPUT] == 20
    assert attrs[sc.USAGE_CACHE_READ] == 900
    assert attrs[sc.USAGE_CACHE_CREATION] == 50
    assert attrs[sc.USAGE_REASONING] == 7


# ──────────────────────────────────────────────── 审批等待


async def test_waiting_for_a_human_is_its_own_span(traced, monkeypatch) -> None:
    """★ 「等人点头」必须能从系统耗时里摘出来（§04）。

    一次 run 里可能有几分钟花在等用户点「允许」上。它与 Run span 是包含
    关系（墙上时钟本来就含它），关键在于**单独可量** —— 否则 P95 高得
    离谱却分不清是系统慢还是人在吃午饭。这两者一个是工程问题、一个是
    产品问题，处置方式完全不同。

    实测撞到过：委派的子智能体等审批等满 300s 直到 adapter 超时。看到
    「await_approval 占了 300s」与看到「Run 耗时 300s」是两种排查起点。
    """
    import atlas_server.services.approval as approval_mod
    from atlas_server.services.approval import RedisApprovalGate

    provider_tracer = traced  # exporter
    import atlas_server.telemetry as telemetry

    monkeypatch.setattr(approval_mod, "_tracer", telemetry.tracer, raising=False)

    class _Session:
        def add(self, _row: object) -> None: ...
        async def flush(self) -> None: ...
        async def execute(self, *_a: object, **_k: object) -> None: ...
        async def commit(self) -> None: ...

    class _Maker:
        def __call__(self) -> _Maker:
            return self

        async def __aenter__(self) -> _Session:
            return _Session()

        async def __aexit__(self, *_exc: object) -> None: ...

    class _Redis:
        async def blpop(self, _keys: list[str], timeout: int = 0) -> tuple[str, str]:
            return ("k", "approved")

    gate = RedisApprovalGate(_Maker(), _Redis(), RUN_ID, timeout_s=5)
    decision = await gate.request(
        approval_id=str(THREAD_ID), tool_name="Write /workspace/a.md", args={}
    )

    assert decision == "approved"
    span = by_name(provider_tracer)["await_approval"]
    assert span.attributes[sc.TOOL_NAME] == "Write /workspace/a.md"
    assert span.attributes[sc.RUN_ID] == str(RUN_ID)
    # 决定要记在 span 上 —— 「批准 / 拒绝 / 过期」的分布是产品侧的指标
    assert span.attributes["atlas.approval.decision"] == "approved"
