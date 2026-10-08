"""委派挂起：图在工具执行完之后跳出，而不是带着占位结果去问模型。

这是「一轮分多段执行」的图内支点。一次委派可能要跑一小时 —— 父 run 不该
在进程里等那么久（部署一次就全丢）。受理方于是返回一个哨兵，本段到此为止，
子 run 跑完后再续跑下一段。

全部脱离 DB 与网络：假模型驱动真实的 kernel 图，注入一个返回哨兵的受理方，
所以测的是真的图行为。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from atlas_engine.contracts import delegation_pending
from atlas_server.domain.events import EventType
from atlas_server.domain.messages import KIND_TOOL_RESULT, Transcript, to_storage
from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec, SubAgentSpec
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from .fakes import TurnModel, tool_call_chunk
from .graphs import run_agent as run

RUN_ID = UUID("66666666-6666-6666-6666-666666666666")
SUB_RUN_ID = "11111111-2222-3333-4444-555555555555"

RESEARCHER = SubAgentSpec(
    name="researcher",
    description="做调研",
    system_prompt="你是研究员。",
    model=ModelSpec(model="claude-sonnet-5"),
)


class PendingGateway:
    """受理方：起了子 run 就返回哨兵，不等结果。"""

    def __init__(self, sub_run_id: str = SUB_RUN_ID) -> None:
        self.sub_run_id = sub_run_id
        self.calls: list[tuple[str, str, bool]] = []

    async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
        self.calls.append((task, name, fresh))
        return delegation_pending(self.sub_run_id)


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
    """第一轮发 task 委派，第二轮给结论 —— 第二轮**不该被走到**。"""
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


async def _collect(
    spec: AgentSpec,
    parent: TurnModel,
    gateway: Any,
    *,
    transcript: Any = None,
    input_content: Any = "帮我查 X",
    **kw: Any,
) -> list:
    return [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=parent,
            input_content=input_content,
            subagents=gateway,
            transcript=transcript,
            **kw,
        )
    ]


# ---------------------------------------------------------------- 跳出


async def test_pending_delegation_stops_the_segment_before_the_next_model_call() -> None:
    """★ 核心性质：哨兵回来之后，模型**不再被调用**。

    不跳出的话模型会看到 `__ATLAS_SUSPENDED__:...` 这串东西并据此
    往下推理 —— 它会把哨兵当成子智能体的结论，然后一本正经地向用户汇报
    一个不存在的结果。
    """
    parent = _delegating_parent()
    events = await _collect(_spec(), parent, PendingGateway())

    assert parent.call_count == 1, "哨兵之后不该再有模型调用"
    assert EventType.RUN_FAILED not in [e.type for e in events]


async def test_the_delegation_still_reports_its_boundary_events() -> None:
    """挂起不是失败：subagent.started 照常发，前端要显示「等待中」。"""
    events = await _collect(_spec(), _delegating_parent(), PendingGateway())
    types = [e.type for e in events]
    assert EventType.SUBAGENT_STARTED in types


async def test_the_sentinel_never_reaches_the_answer() -> None:
    """哨兵不进正文 —— 它是内部标记，不是模型说的话。"""
    events = await _collect(_spec(), _delegating_parent(), PendingGateway())
    completed = [e for e in events if e.type is EventType.MESSAGE_COMPLETED]
    text = "".join(
        block.get("text", "") for e in completed for block in e.data.get("content", [])
    )
    assert "__ATLAS_SUSPENDED__" not in text


# ---------------------------------------------------------------- transcript


async def test_transcript_carries_the_tool_call_and_the_sentinel() -> None:
    """续跑靠的就是这两条：带 tool_use 的 assistant，和哨兵 ToolMessage。

    哨兵那条带着**正确的 tool_call_id**（它是工具节点自己生成的）——
    续跑时把它的内容换成真实结论即可，不需要另一张表去记
    (tool_call_id → sub_run_id) 的对应关系。
    """
    transcript = Transcript()
    await _collect(_spec(), _delegating_parent(), PendingGateway(), transcript=transcript)

    ai = [m for m in transcript.messages if isinstance(m, AIMessage) and m.tool_calls]
    tools = [m for m in transcript.messages if isinstance(m, ToolMessage)]

    assert len(ai) == 1
    assert ai[0].tool_calls[0]["id"] == "call_1"
    assert len(tools) == 1
    assert tools[0].tool_call_id == "call_1"
    assert tools[0].content == delegation_pending(SUB_RUN_ID)


async def test_the_stored_sequence_is_model_legal() -> None:
    """落库的序列本身就能直接喂回模型：tool_use 有配对的 tool_result。"""
    transcript = Transcript()
    await _collect(_spec(), _delegating_parent(), PendingGateway(), transcript=transcript)
    stored = to_storage(transcript.messages)

    uses = [b["id"] for s in stored for b in s.content if b.get("type") == "tool_use"]
    results = [
        b["tool_use_id"] for s in stored for b in s.content if b.get("type") == "tool_result"
    ]
    assert uses == results == ["call_1"]
    assert stored[-1].kind == KIND_TOOL_RESULT


# ---------------------------------------------------------------- 续跑


async def test_resume_does_not_re_append_the_user_question() -> None:
    """★ 续跑时输入已在 history 里，再 append 一遍模型会把同一个问题答两次。"""
    seen: list[list[Any]] = []

    class Recorder(TurnModel):
        # 图跑在 asyncio 上 —— 走的是 _astream，不是 _generate。
        async def _astream(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            async for chunk in super()._astream(messages, *args, **kwargs):
                yield chunk

    model = Recorder(scripts=[[AIMessageChunk(content="结论是 42。")]])
    history = [
        AIMessage(content=[{"type": "text", "text": "我去查一下"}]),
    ]
    await _collect(
        _spec(), model, PendingGateway(), history=history, input_content=None, resume=True
    )

    assert seen, "模型应当被调用"
    humans = [m for m in seen[0] if m.type == "human"]
    assert humans == [], "resume 不该再塞一条用户消息"


async def test_a_non_pending_delegation_still_runs_to_completion() -> None:
    """★ 回归：受理方给真结论时，一切照旧 —— 模型拿到结果继续说话。

    挂起是长委派的路径；短委派必须一个字都不变，否则这次改造就是把
    「委派」整体换了语义。
    """

    class ImmediateGateway:
        async def delegate(self, task: str, name: str, *, fresh: bool = False) -> str:
            return "X 的结论是 42。"

    parent = _delegating_parent()
    events = await _collect(_spec(), parent, ImmediateGateway())

    assert parent.call_count == 2, "拿到真结论后模型要继续"
    assert EventType.SUBAGENT_FINISHED in [e.type for e in events]
    assert EventType.RUN_FINISHED in [e.type for e in events]


# ──────────────────────────────── 当场等待窗口与父 run 的预算无关


async def test_the_inline_wait_is_not_capped_by_the_parent_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 父 run 没有时间上限：当场等多久只由 subagent_inline_wait_s 决定。

    原先窗口取 min(inline_wait, 父 timeout_s) —— 父这一段晚期才委派时，会在当场等待
    途中撞上父的整段超时，父以 run_timeout 失败，子 run 的结论再也回不来。
    """
    from atlas_server.services import subagent as module

    class _Runs:
        def __init__(self, _session: Any) -> None:
            pass

        async def get(self, _run_id: UUID) -> Any:
            return SimpleNamespace(status="running")  # 子 run 一直没跑完

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class _Relay:
        async def is_cancelled(self, _run_id: UUID) -> bool:
            return False

    monkeypatch.setattr(module, "RunRepository", _Runs)
    service = module.SubagentService(
        _Session,  # type: ignore[arg-type]
        SimpleNamespace(subagent_poll_interval_s=0.02, subagent_inline_wait_s=0.3),  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        relay=_Relay(),  # type: ignore[arg-type]
        parent_run_id=uuid4(),
        parent_thread=SimpleNamespace(id=uuid4(), agent_id=uuid4(), created_by=uuid4()),  # type: ignore[arg-type]
        parent_spec=SimpleNamespace(limits=SimpleNamespace(timeout_s=0.01)),  # type: ignore[arg-type]
        agent_version_id=uuid4(),
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await service._await_result("researcher", uuid4(), UUID(SUB_RUN_ID))
    assert loop.time() - started >= 0.25  # 等满了窗口，没有被 0.01s 的 timeout_s 截短
    assert result == delegation_pending(SUB_RUN_ID)  # 等不到就挂起，而不是失败
