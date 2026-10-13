"""TraceEvent.depth = run 在**委派树**里的深度（S1）。

这个字段的语义变过一次，而两次的口径长得一样（都是个小整数），所以最容易
静默出错的地方就在这里：

    旧口径  LangGraph 子图的命名空间层数
    新口径  这个 run 在委派树里的位置（主 run 0，子 run 1）

委派改成「子会话 = thread + 独立子 run」之后图内不再编译子图，旧口径**恒为
0** —— 前端按 depth 缩进的逻辑因此成了死代码。新口径由 EventFactory 的
base_depth 给出，让那套渲染重新有值。

★ 本文件最要紧的一条是 `sub_run_text_is_not_dropped`。runner 里有好几处
  `local_depth == 0` 的判据（丢弃子图的内部输出、只收主图的 transcript），
  它们量的是**局部**深度。把叠加后的 depth 传进去的话，子 run 的正文会被
  整个丢掉 —— 而症状极具迷惑性：run 状态 succeeded、模型确实被调用、
  只是内容凭空消失。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from atlas_server.domain.events import EventFactory, EventType
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec
from langchain_core.messages import AIMessageChunk

from .fakes import TurnModel, text_model
from .graphs import run_agent as run

RUN_ID = UUID("77777777-7777-7777-7777-777777777777")


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "slug": "worker",
        "name": "工人",
        "system_prompt": "你干活。",
        "model": ModelSpec(model="claude-sonnet-5"),
        "tool_names": (),
        "limits": LimitSpec(timeout_s=30),
    }
    base.update(over)
    return AgentSpec(**base)


async def _collect(*, base_depth: int, model: Any = None) -> list:
    return [
        e
        async for e in run(
            _spec(),
            model or text_model("干完了"),
            run_id=RUN_ID,
            input_content="干活",
            base_depth=base_depth,
        )
    ]


# ──────────────────────────────────────────────── EventFactory


def _utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def test_base_depth_is_added_to_every_event() -> None:
    factory = EventFactory(RUN_ID, _utc, base_depth=1)
    assert factory.make(EventType.RUN_STARTED).depth == 1
    assert factory.make(EventType.MESSAGE_DELTA, {"text": "x"}).depth == 1


def test_local_depth_stacks_on_top_of_base_depth() -> None:
    """相加而不是二选一 —— 两者量的是不同的东西。

    局部深度来自图内的子图命名空间，base_depth 来自 run 的父子关系。
    委派模式下局部恒为 0，所以实际取值就是 base_depth；相加这条规则让将来
    真出现图内嵌套时不用回来改。
    """
    factory = EventFactory(RUN_ID, _utc, base_depth=1)
    assert factory.make(EventType.TOOL_STARTED, {}, 1).depth == 2


def test_main_run_events_stay_at_zero() -> None:
    factory = EventFactory(RUN_ID, _utc)  # 默认 base_depth=0
    assert factory.make(EventType.RUN_STARTED).depth == 0


# ──────────────────────────────────────────────── 整条链路


async def test_a_sub_run_marks_all_its_events(fake_model_ok=None) -> None:
    """子 run 产出的每一个事件都带 depth=1 —— 前端据此决定渲染位置。"""
    events = await _collect(base_depth=1)
    assert events, "应当有事件"
    assert all(e.depth == 1 for e in events), [
        (e.type, e.depth) for e in events if e.depth != 1
    ]


async def test_a_main_run_marks_nothing() -> None:
    events = await _collect(base_depth=0)
    assert all(e.depth == 0 for e in events)


async def test_sub_run_text_is_not_dropped() -> None:
    """★ 核心回归：base_depth=1 不能让子 run 的正文消失。

    runner 里那些 `local_depth == 0` 的判据量的是**局部**深度。一旦有人把
    叠加后的 depth 传进去（两个变量长得一样，很容易），子 run 的 delta 会
    在 `_from_messages` 的 `if depth > 0: return []` 那里被整批丢掉。

    那个 bug 不会报错：run 照常 succeeded，模型照常被调用，只是落库的助手
    消息是空的。
    """
    events = await _collect(base_depth=1, model=text_model("子智能体的结论"))

    deltas = [e for e in events if e.type is EventType.MESSAGE_DELTA]
    assert deltas, "子 run 的 message.delta 被丢掉了"
    assert all(e.depth == 1 for e in deltas)

    completed = next(e for e in events if e.type is EventType.MESSAGE_COMPLETED)
    text = "".join(b.get("text", "") for b in completed.data["content"])
    assert text == "子智能体的结论"


async def test_sub_run_tool_calls_are_not_dropped() -> None:
    """工具事件同理 —— 它们走 updates 流，判据也是局部深度。"""
    from .fakes import tool_call_chunk

    model = TurnModel(
        scripts=[
            [tool_call_chunk("write_todos", '{"todos":[]}', "t1")],
            [AIMessageChunk(content="好了")],
        ]
    )
    events = await _collect(base_depth=1, model=model)

    tool_events = [
        e
        for e in events
        if e.type in (EventType.TOOL_STARTED, EventType.TOOL_COMPLETED)
    ]
    assert tool_events, "子 run 的工具事件被丢掉了"
    assert all(e.depth == 1 for e in tool_events)
