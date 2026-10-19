"""SubAgent 挂起：起了子 run 就让本段结束，子 run 跑完续跑时把结论填进去。

一次 SubAgent 可能要跑一小时 —— 父 run 不该在进程里等那么久（部署一次就全丢）。
SubAgentMiddleware 因此照审批的姿态接管 `task`：问 spawner 要一个子 run（立刻返回），
在 state.waiting_on 里登记一项（**不写 tool 消息**），本段到此为止；续跑时结论从
decisions 进来，作为 `task` 的结果交出（contracts/suspension.py）。

全部脱离 DB 与网络：假模型驱动真实的 kernel 图，注入一个假的 spawner，
所以测的是真的图行为。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from atlas_agent.domain.events import EventType
from atlas_agent.domain.messages import KIND_TOOL_RESULT, Transcript, to_storage
from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec, SubAgentSpec
from atlas_engine.contracts import ApprovalState
from atlas_engine.kernel.middleware.approval import approval_id_for
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from .fakes import Spawner, TurnModel, tool_call_chunk
from .graphs import run_agent as run
from .graphs import run_to_end

RUN_ID = UUID("66666666-6666-6666-6666-666666666666")

RESEARCHER = SubAgentSpec(
    name="researcher",
    description="做调研",
    system_prompt="你是研究员。",
    model=ModelSpec(model="claude-sonnet-5"),
)


def _spec(**over: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "slug": "delegator",
        "name": "派活的",
        "system_prompt": "你会派 SubAgent。",
        "model": ModelSpec(model="claude-sonnet-5"),
        "tool_names": ("task",),
        "subagents": (RESEARCHER,),
        "limits": LimitSpec(timeout_s=30),
    }
    base.update(over)
    return AgentSpec(**base)


def _task(call_id: str, name: str = "researcher", brief: str = "查一下 X") -> AIMessageChunk:
    args = json.dumps({"description": brief, "subagent_type": name}, ensure_ascii=False)
    return tool_call_chunk("task", args, call_id)


def _parent() -> TurnModel:
    """第一轮派 SubAgent，第二轮（续跑段）给结论。"""
    return TurnModel(scripts=[[_task("call_1")], [AIMessageChunk(content="子智能体查完了。")]])


async def _segment(spec: AgentSpec, model: TurnModel, spawner: Any, **kw: Any) -> list:
    """只跑一段（不续跑）。"""
    kw.setdefault("input_content", "帮我查 X")
    return [e async for e in run(spec, run_id=RUN_ID, model=model, spawner=spawner, **kw)]


# ---------------------------------------------------------------- 挂起


async def test_spawning_stops_the_segment_before_the_next_model_call() -> None:
    """★ 核心性质：起了子 run 之后，模型**不再被调用**，本段以 run.suspended 结束。

    不跳出的话模型拿到的是一个悬空的 tool_call —— provider 直接 400。
    """
    parent = _parent()
    events = await _segment(_spec(), parent, Spawner())

    assert parent.call_count == 1, "挂起之后不该再有模型调用"
    assert events[-1].type is EventType.RUN_SUSPENDED
    assert EventType.RUN_FAILED not in [e.type for e in events]


async def test_the_suspension_records_which_call_waits_on_which_sub_run() -> None:
    """续跑靠两份依据：transcript 里带 tool_use 的 assistant（**没有**结果），和
    run.suspended 里的等待项 —— 哪个 tool_call 在等哪个子 run。"""
    transcript = Transcript()
    events = await _segment(_spec(), _parent(), Spawner(), transcript=transcript)

    ai = [m for m in transcript.messages if isinstance(m, AIMessage) and m.tool_calls]
    assert len(ai) == 1 and ai[0].tool_calls[0]["id"] == "call_1"
    assert not [m for m in transcript.messages if isinstance(m, ToolMessage)]
    assert events[-1].data["waiting_on"] == [
        {"tool_call_id": "call_1", "reason": "subagent", "token": "sub-1"}
    ]


async def test_the_card_stays_running_until_the_conclusion_arrives() -> None:
    """subagent.started 照常发；subagent.finished 要等续跑填进结论才发。

    卡片在这之间是「运行中」。
    """
    first = await _segment(_spec(), _parent(), Spawner())
    types = [e.type for e in first]
    assert EventType.SUBAGENT_STARTED in types
    assert EventType.SUBAGENT_FINISHED not in types

    events = await run_to_end(
        _spec(), _parent(), run_id=RUN_ID, input_content="帮我查 X", spawner=Spawner("42")
    )
    finished = next(e for e in events if e.type is EventType.SUBAGENT_FINISHED)
    assert finished.data["result"] == "42"
    assert not finished.data["failed"]


async def test_no_internal_marker_reaches_the_event_stream() -> None:
    """挂起不靠任何哨兵字符串 —— 事件里不该出现内部标记。"""
    events = await run_to_end(
        _spec(), _parent(), run_id=RUN_ID, input_content="帮我查 X", spawner=Spawner()
    )
    blob = json.dumps([e.data for e in events], ensure_ascii=False, default=str)
    assert "__ATLAS_" not in blob


async def test_the_waiting_call_is_stored_dangling() -> None:
    """★ 落库时在等的调用**不补**「中断」—— 续跑后它才拿到结果，补了就是两份。

    不在等的悬空照旧补齐：那是 to_storage 的不变量（每个 tool_use 都有结果）。
    """
    transcript = Transcript()
    await _segment(_spec(), _parent(), Spawner(), transcript=transcript)

    def pairs(stored: list[Any]) -> tuple[list[str], list[str]]:
        uses = [b["id"] for s in stored for b in s.content if b.get("type") == "tool_use"]
        results = [
            b["tool_use_id"] for s in stored for b in s.content if b.get("type") == "tool_result"
        ]
        return uses, results

    assert pairs(to_storage(transcript.messages, waiting={"call_1"})) == (["call_1"], [])
    sealed = to_storage(transcript.messages)
    assert pairs(sealed) == (["call_1"], ["call_1"])
    assert sealed[-1].kind == KIND_TOOL_RESULT


# ---------------------------------------------------------------- 续跑


async def test_resume_fills_the_conclusion_without_spawning_again() -> None:
    """★ 续跑：消息原样 + waiting_on + decisions → 结论直接填进 `task` 的结果，**不再起子 run**。"""
    spawner = Spawner("X 的结论是 42。")
    model = TurnModel(scripts=[[AIMessageChunk(content="子智能体说是 42。")]])
    history = [
        HumanMessage(content="帮我查 X"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "task",
                    "args": {"description": "查一下 X", "subagent_type": "researcher"},
                }
            ],
        ),
    ]
    transcript = Transcript()
    events = await _segment(
        _spec(),
        model,
        spawner,
        transcript=transcript,
        history=history,
        input_content=None,
        resume=True,
        waiting_on={"call_1": {"reason": "subagent", "token": "sub-9"}},
        decisions={"sub-9": "X 的结论是 42。"},
    )

    assert spawner.calls == [], "续跑时又起了一个子 run"
    assert model.call_count == 1
    assert events[-1].type is EventType.RUN_FINISHED
    results = [m for m in transcript.messages if isinstance(m, ToolMessage)]
    assert [(m.tool_call_id, m.content) for m in results] == [("call_1", "X 的结论是 42。")]


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
    history = [AIMessage(content=[{"type": "text", "text": "我去查一下"}])]
    await _segment(_spec(), model, Spawner(), history=history, input_content=None, resume=True)

    assert seen, "模型应当被调用"
    assert [m for m in seen[0] if m.type == "human"] == [], "resume 不该再塞一条用户消息"


async def test_a_finished_sibling_is_not_spawned_again_on_resume() -> None:
    """★ 一批两个 task：一个派不出去（当场回失败结果），一个挂起。续跑跳 tools 时 ToolNode
    会把整批再执行一遍 —— 已有结果的那个由外层的 SuspensionMiddleware 原样返回，
    不会走到 SubAgentMiddleware 去再派一次（它排在调用方中间件之后，create_deep_agent）。"""
    spawner = Spawner("结论")
    parent = TurnModel(
        scripts=[[_task("c1", name="nobody"), _task("c2")], [AIMessageChunk(content="好了。")]]
    )
    events = await run_to_end(
        _spec(), parent, run_id=RUN_ID, input_content="派两个", spawner=spawner
    )

    assert spawner.calls == [("researcher", "查一下 X", False)]
    finished = [e for e in events if e.type is EventType.SUBAGENT_FINISHED]
    # 每个调用恰好一个结论
    assert sorted(e.data["subagent_run_id"] for e in finished) == ["c1", "c2"]
    assert next(e for e in finished if e.data["subagent_run_id"] == "c1").data["failed"]
    assert events[-1].type is EventType.RUN_FINISHED


# ---------------------------------------------------------------- 派不出去


async def test_an_unknown_subagent_fails_on_the_spot_without_suspending() -> None:
    """名录里没有的 → 当场回一条失败结果，不起子 run、不挂起，模型接着说话。"""
    spawner = Spawner()
    parent = TurnModel(
        scripts=[[_task("c1", name="nobody")], [AIMessageChunk(content="那我自己来。")]]
    )
    events = await _segment(_spec(), parent, spawner)

    assert spawner.calls == []
    assert parent.call_count == 2
    finished = next(e for e in events if e.type is EventType.SUBAGENT_FINISHED)
    assert finished.data["failed"] and "不存在" in finished.data["result"]
    assert events[-1].type is EventType.RUN_FINISHED


async def test_a_rejected_spawn_is_a_failed_tool_result_not_a_failed_run() -> None:
    """撞上限、同轮同名、会话被占：spawner 抛的错回成失败的工具结果，run 继续 ——
    一次派不出去是可恢复的局部问题，模型可以改派、合并任务或自己动手。"""
    spawner = Spawner(error=RuntimeError("这一轮已派出 6 个 SubAgent，达到上限 6"))
    parent = TurnModel(scripts=[[_task("c1")], [AIMessageChunk(content="那我自己来。")]])
    events = await _segment(_spec(), parent, spawner)

    finished = next(e for e in events if e.type is EventType.SUBAGENT_FINISHED)
    assert finished.data["failed"] and "达到上限" in finished.data["result"]
    assert events[-1].type is EventType.RUN_FINISHED


# ---------------------------------------------------------------- 与审批的配合


class ScriptedGate:
    def __init__(self, state: ApprovalState) -> None:
        self.state = state
        self.asked = 0

    async def check(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any]
    ) -> ApprovalState:
        self.asked += 1
        return self.state


async def test_a_guarded_task_is_approved_before_the_sub_run_starts() -> None:
    """★ `task` 被配成需审批：先审批、批准后才起子 run —— 等待从 approval 换成 subagent。

    SubAgentMiddleware 排在 ApprovalMiddleware 之内，起子 run 是最后一步；换等待项时
    审批中间件的「删掉这一项」不能把新登记的 subagent 等待项抹掉（release_wait）。
    """
    spec = _spec(limits=LimitSpec(timeout_s=30, require_approval_for=frozenset({"task"})))
    spawner = Spawner()
    parent = _parent()

    # 段 1：审批 pending → 不起子 run，只等审批
    first = await _segment(spec, parent, spawner, approvals=ScriptedGate("pending"))
    assert spawner.calls == []
    assert first[-1].data["waiting_on"] == [
        {"tool_call_id": "call_1", "reason": "approval", "token": approval_id_for("", "call_1")}
    ]

    # 段 2：批准 → 起子 run，等待换成 subagent
    history = [
        HumanMessage(content="帮我查 X"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "task",
                    "args": {"description": "查一下 X", "subagent_type": "researcher"},
                }
            ],
        ),
    ]
    token = approval_id_for("", "call_1")
    second = await _segment(
        spec,
        parent,
        spawner,
        approvals=ScriptedGate("pending"),
        history=history,
        input_content=None,
        resume=True,
        waiting_on={"call_1": {"reason": "approval", "token": token}},
        decisions={token: "approved"},
    )
    assert spawner.calls == [("researcher", "查一下 X", False)]
    assert second[-1].type is EventType.RUN_SUSPENDED
    assert second[-1].data["waiting_on"] == [
        {"tool_call_id": "call_1", "reason": "subagent", "token": "sub-1"}
    ]
