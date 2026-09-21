"""P5：委派的事件边界、用量、标题生成。

全部脱离 DB 与网络 —— 用假模型驱动真实的 kernel 图，注入一个假的
`DelegationProtocol` 受理方，所以测的是真的图行为，不是 mock 出来的行为。

## 图内路径的测试去哪了

engine 层的图内子智能体装配（_resolve_submodels / _subagent_dict）已整体
删除 —— 它在生产不可达（assembly 对配了子智能体的 run 恒注入受理方），
却带着一套只被测试走到的活语义。原先钉在它上面的四条保证没有消失，
而是换了住处：

  子图审批不可绕过   → 子 run 继承 require_approval_for
                       （test_subagent_spec::test_limits_are_inherited...）；
                       子 run 是普通 run，普通 run 的审批已有测试
  子图步数刹车       → 同上：max_steps 随 limits 整体继承，普通 run 的
                       StepLimit 在 test_limits.py 里钉着
  子图工具白名单     → 子 run 用 spec_for_subagent 派生的 tool_names 走
                       build_graph 主路径 —— 白名单由 test_tool_gating.py
                       的主路径测试钉着
  内部步骤 depth=1   → 子 run 有自己的事件流（depth=0），父只看边界事件；
                       端到端见 test_subagent_sessions.py

这正是「子会话 = thread」的回报：子智能体的治理不再需要一套平行的
子图机制，普通 run 的全部守卫自动生效。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_engine.contracts import InvalidSpec
from atlas_server.domain.events import EventType
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec, SubAgentSpec
from atlas_server.domain.tool_registry import unsupported_tools
from langchain_core.messages import AIMessageChunk

from .fakes import TurnModel, tool_call_chunk
from .graphs import build_graph, run_agent as run

RUN_ID = UUID("55555555-5555-5555-5555-555555555555")

RESEARCHER = SubAgentSpec(
    name="researcher",
    description="做调研",
    system_prompt="你是研究员。",
    model=ModelSpec(model="claude-sonnet-5"),
)


class Gateway:
    """DelegationProtocol 的测试实现：按脚本返回，并记录收到的委派。"""

    def __init__(self, reply: str = "子结论") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, bool]] = []

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        self.calls.append((task, name, fresh))
        return self.reply


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "slug": "delegator",
        "name": "委派者",
        "system_prompt": "你会委派任务。",
        "model": ModelSpec(model="claude-sonnet-5"),
        "tool_names": ("task",),
        "subagents": (RESEARCHER,),
        "limits": LimitSpec(timeout_s=30),
    }
    base.update(over)
    return AgentSpec(**base)


def _delegating_parent() -> TurnModel:
    """第一轮发 task 委派，第二轮给结论。"""
    return TurnModel(
        scripts=[
            [
                tool_call_chunk(
                    "task", '{"description":"查一下 X","subagent_type":"researcher"}', "call_1"
                )
            ],
            [AIMessageChunk(content="子智能体查完了。")],
        ]
    )


async def _collect(spec: AgentSpec, parent: TurnModel, gateway: Gateway, **hook_kw: Any) -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=parent,
            input_content="帮我查 X",
            subagents=gateway, **hook_kw,
        )
    ]


# ---------------------------------------------------------------- 委派


async def test_delegation_emits_subagent_boundary_events() -> None:
    """★ P5 完成标准：SubAgentCard 有数据 —— started/finished 成对且可配对。"""
    events = await _collect(_spec(), _delegating_parent(), Gateway("X 的结论是 42。"))
    types = [e.type for e in events]

    assert EventType.RUN_FAILED not in types, next(
        e.data for e in events if e.type is EventType.RUN_FAILED
    )
    assert EventType.SUBAGENT_STARTED in types
    assert EventType.SUBAGENT_FINISHED in types

    started = next(e for e in events if e.type is EventType.SUBAGENT_STARTED)
    finished = next(e for e in events if e.type is EventType.SUBAGENT_FINISHED)

    assert started.data["name"] == "researcher"
    assert started.data["task"] == "查一下 X"
    # 用 tool_call_id 配对 —— 它同时出现在 task 的调用与返回上
    assert started.data["subagent_run_id"] == finished.data["subagent_run_id"] == "call_1"


async def test_the_gateway_receives_the_brief() -> None:
    """任务书原样到达受理方 —— 这是委派唯一携带的输入（设计 §02）。"""
    gateway = Gateway()
    await _collect(_spec(), _delegating_parent(), gateway)
    assert gateway.calls == [("查一下 X", "researcher", False)]


async def test_task_is_not_reported_as_plain_tool_call() -> None:
    """委派不该在工具页里显示成一次普通的 `task` 调用。"""
    events = await _collect(_spec(), _delegating_parent(), Gateway())

    tool_names = [
        e.data.get("name")
        for e in events
        if e.type in (EventType.TOOL_STARTED, EventType.TOOL_COMPLETED)
    ]
    assert "task" not in tool_names


async def test_delegation_result_does_not_pollute_main_answer() -> None:
    """受理方返回的结论进 ToolMessage，不进主回复。

    主回复必须完全来自主模型自己的输出 —— 委派结果混进 message.delta 的话，
    用户会看到一段突然冒出来、语气都不一样的话。
    """
    events = await _collect(_spec(), _delegating_parent(), Gateway("【子智能体的内部输出】"))

    final = next(e for e in events if e.type is EventType.MESSAGE_COMPLETED)
    text = final.data["content"][0]["text"]
    assert "子智能体的内部输出" not in text
    assert text == "子智能体查完了。"

    deltas = [e for e in events if e.type is EventType.MESSAGE_DELTA]
    assert all(e.depth == 0 for e in deltas)


async def test_usage_is_summed_across_all_model_calls() -> None:
    """★ 累加而非覆盖。

    一轮对话里主模型被调用多次（每次工具往返一次）。只留最后一次的话
    token 上限形同虚设。子智能体的用量不在此列 —— 它记在**子 run 自己**
    的行上，这正是「委派 = 子会话上的一个 run」的账目含义。
    """
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    parent = TurnModel(
        scripts=[
            [
                tool_call_chunk("task", '{"description":"t","subagent_type":"researcher"}', "c1"),
                AIMessageChunk(content="", usage_metadata=usage),
            ],
            [AIMessageChunk(content="好了", usage_metadata=usage)],
        ]
    )

    events = await _collect(_spec(), parent, Gateway())
    u = next(e.data for e in events if e.type is EventType.USAGE_UPDATED)

    # 父自己的 2 次调用，各 110 —— 子智能体的账不混进来
    assert u["total_tokens"] == 220, f"实际 {u['total_tokens']}，说明用量被覆盖而非累加"


# ---------------------------------------------------------------- 装配纪律


def test_subagents_without_gateway_is_a_loud_error() -> None:
    """§13.2 不允许静默降级：配了子智能体却没注入受理方必须报错。

    静默砍掉 task 的话，模型按「没有帮手」的方式做事，而配置界面显示它有 ——
    两边都看不出异常。装配移到 server 之后失败点也随之前移：build_graph
    在装配时就炸（run 根本不会开始），比跑起来才发 run.failed 更早、更准。
    """
    import pytest

    with pytest.raises(InvalidSpec, match="受理方"):
        build_graph(_spec(), _delegating_parent())


# ---------------------------------------------------------------- 工具目录


def test_task_reported_unsupported_when_no_subagents_configured() -> None:
    """勾了 task 却没配子智能体 = 等于没开，必须报出来。"""
    assert unsupported_tools(_spec(subagents=())) == ["task"]
    assert unsupported_tools(_spec()) == []


# ---------------------------------------------------------------- 标题生成


async def test_title_emitted_before_run_finished() -> None:
    """§8.1：SSE 在 run.finished 后就关了，标题必须在这之前发出。"""

    async def titler(_text: str) -> dict[str, Any]:
        return {"title": "关于 X 的调研", "degraded": False}

    events = await _collect(_spec(), _delegating_parent(), Gateway(), titler=titler)
    types = [e.type for e in events]

    assert EventType.TITLE_GENERATED in types
    assert types.index(EventType.TITLE_GENERATED) < types.index(EventType.RUN_FINISHED)
    assert types.index(EventType.MESSAGE_COMPLETED) < types.index(EventType.TITLE_GENERATED)


async def test_title_failure_does_not_fail_the_run() -> None:
    """标题生成失败只是没有标题，不该把整个 run 拖垮。"""

    async def broken(_text: str) -> dict[str, Any]:
        raise RuntimeError("haiku 挂了")

    events = await _collect(_spec(), _delegating_parent(), Gateway(), titler=broken)
    types = [e.type for e in events]

    assert EventType.TITLE_GENERATED not in types
    assert types[-1] is EventType.RUN_FINISHED


async def test_seq_still_gapless_with_subagents() -> None:
    """契约规则 2 在委派事件混排下仍然成立。"""
    events = await _collect(_spec(), _delegating_parent(), Gateway())
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), f"seq 不连续：{seqs}"


async def test_text_before_a_tool_call_is_a_separate_block() -> None:
    """★ 调工具前说的话与最终回答，必须是**两段**，不能拼成一条。

    模型常常先说一句「我去委派一下」再调工具，拿到结果后才给结论 ——
    这是两轮发言。早先 runner 把整个 run 的文本 `"".join` 成一条，于是
    用户看到的是：

        I'll delegate the file creation to the cli-hand subagent,
        then read it back myself.已完成。- 我委派了子智能体 cli-hand…

    过程性发言与结论粘在一起，分不出哪句才是答案（两轮分开生成，常常
    还一中一英）。真实委派会话上稳定复现。

    段的边界是**工具调用**，不是换行或段落 —— 见 domain/events.py::Answer。
    """
    parent = TurnModel(
        scripts=[
            [
                AIMessageChunk(content="我先委派一下。"),
                tool_call_chunk(
                    "task", '{"description":"查 X","subagent_type":"researcher"}', "c1"
                ),
            ],
            [AIMessageChunk(content="查完了，结论是 42。")],
        ]
    )
    events = await _collect(_spec(), parent, Gateway("X 的结论是 42。"))

    completed = next(e for e in events if e.type is EventType.MESSAGE_COMPLETED)
    assert completed.data["content"] == [
        {"type": "text", "text": "我先委派一下。"},
        {"type": "text", "text": "查完了，结论是 42。"},
    ]

    # delta 上要带段号，否则前端只能等 message.completed 才分得开，
    # 流式过程中仍然是糊在一起的
    deltas = [e for e in events if e.type is EventType.MESSAGE_DELTA]
    assert {d.data["block"] for d in deltas} == {0, 1}
    assert [d.data["text"] for d in deltas if d.data["block"] == 0] == ["我先委派一下。"]


async def test_a_run_without_tool_calls_stays_one_block() -> None:
    """没调过工具就只有一段 —— 不要凭空把普通回答切碎。"""
    parent = TurnModel(scripts=[[AIMessageChunk(content="直接回答你。")]])
    events = await _collect(_spec(), parent, Gateway())
    completed = next(e for e in events if e.type is EventType.MESSAGE_COMPLETED)
    assert completed.data["content"] == [{"type": "text", "text": "直接回答你。"}]
