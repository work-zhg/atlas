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
    from atlas_server.telemetry import make_id_generator
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    # 与 setup_telemetry 同一个 IdGenerator：trace id = run id 要在这里也成立
    provider = TracerProvider(id_generator=make_id_generator())
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

    settings = Settings(litellm_key="x", default_user_id=RUN_ID, database_url="x", redis_url="x")
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
        spans["chat deepseek-chat"].parent.span_id == spans["invoke_agent analyst"].context.span_id
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
        for e in events_of((EventType.TOOL_STARTED, {"call_id": "inner", "name": "grep"}), depth=1):
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
    assert attrs[sc.ATLAS_USAGE_INPUT] == 100
    assert attrs[sc.ATLAS_USAGE_OUTPUT] == 20
    assert attrs[sc.ATLAS_USAGE_CACHE_READ] == 900
    assert attrs[sc.ATLAS_USAGE_CACHE_CREATION] == 50
    assert attrs[sc.ATLAS_USAGE_REASONING] == 7
    # ★ 根 span 不带 gen_ai.usage.*：它不是一次模型调用，带了会被当成 Generation 重复计费
    assert not any(k.startswith("gen_ai.usage.") for k in attrs)


# ──────────────────────────────────────────────── 审批等待


async def test_requesting_an_approval_is_recorded_as_a_span(traced, monkeypatch) -> None:
    """★ 「等人点头」必须能从系统耗时里摘出来（§04）。

    一次 run 里可能有几分钟 —— 改成挂起之后甚至可能是**几小时** —— 花在等
    用户点「允许」上。混在延迟统计里的话 P95 高得离谱却分不清是系统慢还是人
    在吃午饭。这两者一个是工程问题、一个是产品问题，处置方式完全不同。

    ★ 度量形态随机制变了（S7）。原先审批是一段在线阻塞，于是有一个
      `await_approval` span 恰好覆盖那段墙上时钟，决定也记在它上面。现在
      `check()` 立刻返回 —— **没有那段时间可量了**，等待发生在进程之外。

      所以这里只断言「请求被记下来了」。完整的等待时长要跨段算：
      run.suspended 事件的时间戳到续跑段 run.started 的时间戳，两者在同一条
      会话流上、同一个 run_id 下。那是个查询，不是一个 span —— 而它本来就
      更准（含进程重启的那段）。
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

    class _Approvals:
        """首次查询：库里还没有这条。"""

        def __init__(self, _session: object) -> None: ...
        async def get(self, _aid: object) -> None:
            return None

        async def create(self, **_kw: object) -> None: ...

    monkeypatch.setattr(approval_mod, "ApprovalRepository", _Approvals)

    class _Runs:
        def __init__(self, _session: object) -> None: ...
        async def set_status(self, *_a: object) -> None: ...

    monkeypatch.setattr(approval_mod, "RunRepository", _Runs)

    # ★ 没有 timeout_s 了：超时判定搬到恢复扫描（S8）
    gate = RedisApprovalGate(_Maker(), None, RUN_ID)
    state = await gate.check(approval_id=str(THREAD_ID), tool_name="Write /workspace/a.md", args={})

    assert state == "pending"
    span = by_name(provider_tracer)["approval_requested"]
    assert span.attributes[sc.TOOL_NAME] == "Write /workspace/a.md"
    assert span.attributes[sc.RUN_ID] == str(RUN_ID)


# ═════════════════════ Langfuse 接入（doc/langfuse-integration-design.html）═════════════════════

ROOT_THREAD = UUID("99999999-9999-9999-9999-999999999999")
USER = UUID("11111111-2222-3333-4444-555555555555")


class _Thread:
    def __init__(self, *, parent: UUID | None = None) -> None:
        self.id = THREAD_ID
        self.created_by = USER
        self.stream_thread_id = parent or THREAD_ID


class _Run:
    """带 thread / 输入 / 深度的 PreparedRun 替身。"""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        parent_thread: UUID | None = None,
        text: str = "在工作区写一个 hello.py",
        resume: bool = False,
        base_depth: int = 0,
    ) -> None:
        self.spec = spec
        self.thread = _Thread(parent=parent_thread)
        self.thread_id = THREAD_ID
        self.input_content = text
        self.resume = resume
        self.base_depth = base_depth


@pytest.fixture
def capture(monkeypatch):
    """设置内容档位（off / io / full）。"""

    def set_level(level: str) -> None:
        import atlas_server.telemetry.content as content_mod

        monkeypatch.setattr(content_mod, "level", lambda: level)

    set_level("off")
    return set_level


def test_trace_id_is_the_run_id_and_segments_share_it(traced) -> None:
    """★ 一个 run 挂起后续跑是两段执行，必须落进同一条 trace（P2）；trace id 就是 run id。"""
    with RunTrace.start(_Run(make_spec()), run_id=str(RUN_ID)):
        pass
    with RunTrace.start(_Run(make_spec(), resume=True), run_id=str(RUN_ID)):
        pass
    roots = traced.get_finished_spans()
    assert len(roots) == 2
    assert {s.context.trace_id for s in roots} == {RUN_ID.int}
    # ★ 真正的根：没有父（V5 实测，虚拟父会在 Langfuse 里留下指向不存在节点的孤儿）
    assert all(s.parent is None for s in roots)
    assert [s.attributes[sc.RESUMED] for s in roots] == [False, True]


def test_session_is_the_root_thread_and_user_is_the_owner(traced) -> None:
    """★ 子 run 在子会话上，但归到根会话那个 Session（P1）；用户 = 会话所有者（P9）。"""
    parent_run = "12121212-1212-1212-1212-121212121212"
    with RunTrace.start(
        _Run(make_spec("acp"), parent_thread=ROOT_THREAD, base_depth=1),
        run_id=str(RUN_ID),
        parent_run_id=parent_run,
    ):
        pass
    attrs = traced.get_finished_spans()[0].attributes
    assert attrs[sc.LF_SESSION_ID] == str(ROOT_THREAD)
    assert attrs[sc.CONVERSATION_ID] == str(ROOT_THREAD)
    assert attrs[sc.THREAD_ID] == str(THREAD_ID)  # 真实所在的子会话不丢
    assert attrs[sc.LF_USER_ID] == str(USER)
    assert attrs[sc.PARENT_RUN_ID] == parent_run
    assert attrs[f"{sc.LF_TRACE_METADATA}parent_run_id"] == parent_run
    assert attrs[sc.LF_TRACE_NAME] == "analyst"
    assert list(attrs[sc.LF_TRACE_TAGS]) == ["acp", "analyst"]
    assert attrs[sc.LF_OBSERVATION_TYPE] == sc.OBS_AGENT


def test_child_run_keeps_its_own_steps(traced) -> None:
    """★ P10：子 run 自己的事件 depth = 1（base_depth），原先被当成别人的内部步骤整个丢掉。"""
    run = _Run(make_spec("acp"), parent_thread=ROOT_THREAD, base_depth=1)
    with RunTrace.start(run, run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Write"}),
            (EventType.TOOL_COMPLETED, {"call_id": "c1", "status": "success"}),
            depth=1,
        ):
            rt.observe(e)
        for e in events_of((EventType.TOOL_STARTED, {"call_id": "c2", "name": "x"}), depth=2):
            rt.observe(e)  # 更深一层仍然是别人的
    names = set(by_name(traced))
    assert "execute_tool Write" in names
    assert "execute_tool x" not in names


def test_acp_turn_usage_becomes_one_synthetic_generation(traced) -> None:
    """★ CLI 内部的模型调用看不到：用一轮的总用量合成一个 Generation 计费（P5）。"""
    with RunTrace.start(_Run(make_spec("acp")), run_id=str(RUN_ID)) as rt:
        rt.observe(
            events_of(
                (
                    EventType.USAGE_UPDATED,
                    {
                        "input_tokens": 19659,
                        "output_tokens": 278,
                        "cache_read": 57856,
                        "model": "deepseek-chat",
                    },
                )
            )[0]
        )
    spans = by_name(traced)
    gen = spans["chat deepseek-chat"]
    assert gen.attributes[sc.LF_OBSERVATION_TYPE] == sc.OBS_GENERATION
    assert gen.attributes[sc.REQUEST_MODEL] == "deepseek-chat"
    assert gen.attributes[sc.USAGE_INPUT] == 19659
    assert gen.attributes[sc.USAGE_CACHE_READ] == 57856
    assert gen.attributes[sc.USAGE_SCOPE] == "turn"
    assert gen.parent.span_id == spans["invoke_agent analyst"].context.span_id
    # 根只有展示用的累计值
    assert not any(k.startswith("gen_ai.usage.") for k in spans["invoke_agent analyst"].attributes)


def test_native_usage_does_not_synthesize_a_generation(traced) -> None:
    """native 按次计费（model_callback 的 chat span），不合成，免得算两遍。"""
    with RunTrace.start(_Run(make_spec()), run_id=str(RUN_ID)) as rt:
        rt.observe(events_of((EventType.USAGE_UPDATED, {"input_tokens": 5}))[0])
    assert list(by_name(traced)) == ["invoke_agent analyst"]


def test_off_level_writes_no_content_at_all(traced, capture) -> None:
    capture("off")
    with RunTrace.start(_Run(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Bash", "args": {"cmd": "ls"}}),
            (EventType.TOOL_COMPLETED, {"call_id": "c1", "result_preview": "a.txt"}),
            (EventType.MESSAGE_COMPLETED, {"content": [{"type": "text", "text": "好了"}]}),
        ):
            rt.observe(e)
    for span in traced.get_finished_spans():
        assert not set(span.attributes) & set(sc.CONTENT_ATTRIBUTES), span.name


def test_io_level_records_trace_and_tool_io(traced, capture) -> None:
    capture("io")
    with RunTrace.start(_Run(make_spec()), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Bash", "args": {"cmd": "ls"}}),
            (EventType.TOOL_COMPLETED, {"call_id": "c1", "result_preview": "a.txt"}),
            (EventType.MESSAGE_COMPLETED, {"content": [{"type": "text", "text": "好了"}]}),
        ):
            rt.observe(e)
    spans = by_name(traced)
    root = spans["invoke_agent analyst"].attributes
    assert root[sc.LF_TRACE_INPUT] == "在工作区写一个 hello.py"
    assert root[sc.LF_TRACE_OUTPUT] == "好了"
    tool = spans["execute_tool Bash"].attributes
    assert tool[sc.LF_OBSERVATION_INPUT] == '{"cmd": "ls"}'
    assert tool[sc.LF_OBSERVATION_OUTPUT] == "a.txt"


def test_resumed_segment_does_not_repeat_the_input(traced, capture) -> None:
    capture("io")
    with RunTrace.start(_Run(make_spec(), resume=True), run_id=str(RUN_ID)):
        pass
    assert sc.LF_TRACE_INPUT not in traced.get_finished_spans()[0].attributes


def test_mode_and_denial_become_root_events(traced) -> None:
    with RunTrace.start(_Run(make_spec("acp")), run_id=str(RUN_ID)) as rt:
        for e in events_of(
            (EventType.AGENT_MODE, {"requested": "auto", "effective": "auto", "degraded": False}),
            (EventType.TOOL_STARTED, {"call_id": "c1", "name": "Bash"}),
            (
                EventType.TOOL_FAILED,
                {
                    "call_id": "c1",
                    "error_kind": "auto_mode_denied",
                    "denied_reason": "Unverifiable Deletion Target",
                },
            ),
        ):
            rt.observe(e)
    events = {e.name: e for e in by_name(traced)["invoke_agent analyst"].events}
    assert events["agent.mode"].attributes["effective"] == "auto"
    assert events["tool.denied"].attributes["reason"] == "Unverifiable Deletion Target"
    # Langfuse 不展示 span event（V8）：同时有零时长的 event 类型 span
    spans = by_name(traced)
    for name in ("agent.mode", "tool.denied"):
        assert spans[name].attributes[sc.LF_OBSERVATION_TYPE] == sc.OBS_EVENT
        assert spans[name].start_time == spans[name].end_time


def test_model_call_is_a_generation_with_content_only_at_full(traced, capture) -> None:
    import atlas_server.telemetry.model_callback as mc
    from atlas_server.telemetry import tracer as _tracer  # noqa: F401  （确保模块已加载）
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    handler = mc.ModelSpanHandler()
    for level, expect_content in (("io", False), ("full", True)):
        traced.clear()
        capture(level)
        from uuid import uuid4

        rid = uuid4()
        handler.on_chat_model_start(
            {},
            [[SystemMessage("你是助手"), HumanMessage("我的密钥是 sk-abcdefghijklmnopqrstuvwxyz")]],
            run_id=rid,
            invocation_params={"model": "deepseek-chat"},
        )
        handler.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage("好的"))]]), run_id=rid
        )
        span = traced.get_finished_spans()[0]
        assert span.attributes[sc.LF_OBSERVATION_TYPE] == sc.OBS_GENERATION
        assert (sc.LF_OBSERVATION_INPUT in span.attributes) is expect_content
        if expect_content:
            text = span.attributes[sc.LF_OBSERVATION_INPUT]
            assert "sk-[REDACTED]" in text and "abcdefghijklmnop" not in text  # 脱敏
            assert "好的" in span.attributes[sc.LF_OBSERVATION_OUTPUT]


# ──────────────────────────────────────────────── 内容处理：脱敏、截断、配置兼容


def test_redaction_covers_common_secret_shapes() -> None:
    from atlas_server.telemetry.content import redact

    text = redact(
        "key sk-abcdefghijklmnopqrstuv; aws AKIAABCDEFGHIJKLMNOP; "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123; "
        'API_KEY="supersecretvalue123"; password: hunter2hunter2\n'
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
    )
    for leaked in (
        "abcdefghijklmnopqrstuv",
        "ABCDEFGHIJKLMNOP",
        "abcdefghijklmnopqrstuvwxyz0123",
        "supersecretvalue123",
        "hunter2hunter2",
        "MIIE",
    ):
        assert leaked not in text, leaked
    assert "API_KEY=" in text  # 键名保留，便于辨认


def test_render_truncates_and_respects_level(monkeypatch) -> None:
    import atlas_server.telemetry.content as content_mod

    monkeypatch.setattr(content_mod, "level", lambda: "io")
    monkeypatch.setattr(content_mod, "_max_chars", lambda: 10)
    assert content_mod.render("x" * 25, "io") == "x" * 10 + "…[truncated 15 chars]"
    assert content_mod.render("hello", "full") is None  # 档位不够
    assert content_mod.render({"a": 1}, "io") == '{"a": 1}'


def test_capture_content_accepts_the_old_boolean(monkeypatch) -> None:
    from atlas_server.config import Settings

    for raw, expected in ((True, "full"), (False, "off"), ("true", "full"), ("io", "io")):
        s = Settings(litellm_key="x", default_user_id=USER, otel_capture_content=raw)  # type: ignore[arg-type]
        assert s.otel_capture_content == expected


@pytest.mark.parametrize("config", ["collector.yaml", "collector-langfuse.yaml"])
def test_collector_strips_every_content_attribute_before_jaeger(config: str) -> None:
    """★ server 新增内容字段而 Collector 没跟上 = 内容悄悄进了 Jaeger。两边必须一致。"""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[1] / "deploy/local/otel" / config
    cfg = yaml.safe_load(path.read_text())
    stripped = {a["key"] for a in cfg["processors"]["attributes/strip-content"]["actions"]}
    assert set(sc.CONTENT_ATTRIBUTES) <= stripped
    for name, pipeline in cfg["service"]["pipelines"].items():
        if "otlp_grpc/jaeger" in pipeline["exporters"]:
            assert "attributes/strip-content" in pipeline["processors"], name
