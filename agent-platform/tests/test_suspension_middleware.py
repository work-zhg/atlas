"""挂起的图内支点 —— SuspensionMiddleware 的判据，以及它与 Patch / 审批在真实图里的配合。

图内只看 state 的两个字段（contracts/suspension.py）：

    waiting_on 为空                     → 正常流向 model
    有一项的 token 已在 decisions 里    → jump_to "tools"（去执行 / 填结论）
    都还没有结果                        → jump_to "end"（本段到此为止）

★ 被拦下的调用在消息里**悬空**，不写任何占位（demo/suspend-resume 同一套做法）。
  原先的做法是哨兵 / 放行占位 ToolMessage，续跑时 server 改写历史、before_model 再删
  占位 —— 为了躲开 PatchToolCallsMiddleware 才绕了这一圈（doc/detail/suspension.html
  修正 10、13）。现在 Patch 认 waiting_on，绕路随之消失。

★ 「不用 checkpointer」的实验证据也在这里：输入 = 消息 + waiting_on + decisions，
  一张**新建**的图就能从工具节点开始执行。
"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_engine.contracts import ApprovalState, ToolEffect, merge_waits
from atlas_engine.kernel.middleware.approval import REJECTED_RESULT, approval_id_for
from atlas_engine.kernel.middleware.patch_tool_calls import PatchToolCallsMiddleware
from atlas_engine.kernel.middleware.permission import PermissionMiddleware
from atlas_engine.kernel.middleware.suspension import SuspensionMiddleware, release_wait
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from .fakes import TurnModel, tool_call_chunk


def _call(call_id: str, name: str = "danger") -> dict[str, Any]:
    return {"type": "tool_call", "id": call_id, "name": name, "args": {"path": "/tmp/x"}}


def _wait(reason: str, token: str) -> dict[str, str]:
    return {"reason": reason, "token": token}


def _decide(
    messages: list[Any],
    waits: dict[str, Any] | None = None,
    decisions: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    state: dict[str, Any] = {"messages": messages}
    if waits is not None:
        state["waiting_on"] = waits
    if decisions is not None:
        state["decisions"] = decisions
    return SuspensionMiddleware().before_model(state, None)


_DANGLING = [HumanMessage(content="删掉它"), AIMessage(content="", tool_calls=[_call("c1")])]


# ──────────────────────────────────────────────── 判据本身


def test_no_waits_flows_to_the_model() -> None:
    assert _decide([HumanMessage(content="你好")]) is None


@pytest.mark.parametrize("reason", ["approval", "subagent"])
def test_waits_without_results_jump_to_end(reason: str) -> None:
    """★ 中间件不区分 reason —— SubAgent 和审批走同一条跳出路径。"""
    assert _decide(_DANGLING, {"c1": _wait(reason, "t1")}) == {"jump_to": "end"}


def test_a_settled_wait_jumps_to_tools() -> None:
    out = _decide(_DANGLING, {"c1": _wait("approval", "ap-1")}, {"ap-1": "approved"})
    assert out == {"jump_to": "tools"}


def test_one_settled_wait_is_enough_to_go_to_tools() -> None:
    """有一项等到了就去 tools：没等到的那项会在 tools 里被重新登记，回来时再跳出。"""
    ai = AIMessage(content="", tool_calls=[_call("c1"), _call("c2", "task")])
    waits = {"c1": _wait("approval", "ap-1"), "c2": _wait("subagent", "sub-1")}
    assert _decide([ai], waits, {"ap-1": "approved"}) == {"jump_to": "tools"}


def test_results_for_other_tokens_do_not_release() -> None:
    """decisions 里的结果只对**同一个 token** 的等待项生效。"""
    out = _decide(_DANGLING, {"c1": _wait("approval", "ap-1")}, {"ap-other": "approved"})
    assert out == {"jump_to": "end"}


def test_a_dangling_call_outside_waiting_on_is_not_its_business() -> None:
    """★ 悬空本身不是判据：不在 waiting_on 里的悬空是「被放弃」，由图入口的 Patch 补齐。"""
    assert _decide(_DANGLING) is None


# ──────────────────────────────────────────────── reducer 与 release_wait


def test_merge_waits_deletes_on_none_and_merges_parallel_writes() -> None:
    """★ 一批里几个调用同一步写 waiting_on 要能合并；拿到结果写的 None 要能删掉。"""
    merged = merge_waits({"a": _wait("approval", "1")}, {"b": _wait("subagent", "2")})
    assert merged == {"a": _wait("approval", "1"), "b": _wait("subagent", "2")}
    assert merge_waits(merged, {"a": None}) == {"b": _wait("subagent", "2")}
    assert merge_waits(None, None) == {}


def test_release_wait_wraps_a_tool_message() -> None:
    msg = ToolMessage(content="ok", tool_call_id="c1")
    out = release_wait(msg, "c1")
    assert out.update == {"messages": [msg], "waiting_on": {"c1": None}}


def test_release_wait_keeps_a_commands_own_update() -> None:
    """工具自己返回 Command 时，删除这一项要并进它的更新，不能覆盖掉。"""
    msg = ToolMessage(content="ok", tool_call_id="c1")
    out = release_wait(Command(update={"messages": [msg], "todos": [1]}), "c1")
    assert out.update == {"messages": [msg], "todos": [1], "waiting_on": {"c1": None}}

    listed = release_wait(Command(update=[("messages", [msg])]), "c1")
    assert listed.update == [("waiting_on", {"c1": None}), ("messages", [msg])]


def test_release_wait_lets_a_new_wait_for_the_same_call_win() -> None:
    """★ task 被配成需审批：批准后 SubAgentMiddleware 起了子 run，等待从 approval 换成
    subagent。审批中间件的「删掉这一项」不能把新登记的等待项抹掉。"""
    handover = Command(update={"waiting_on": {"c1": _wait("subagent", "sub-1")}})
    assert release_wait(handover, "c1").update == {"waiting_on": {"c1": _wait("subagent", "sub-1")}}


# ──────────────────────────────────────────────── 图内的真实行为（与 Patch / 审批一起）


class ScriptedGate:
    """按预设状态回应；记录被问过几次。"""

    def __init__(self, state: ApprovalState) -> None:
        self.state = state
        self.asked = 0

    async def check(
        self, *, approval_id: str, tool_name: str, args: dict[str, Any], **_: Any
    ) -> ApprovalState:
        self.asked += 1
        return self.state


def _agent(model: TurnModel, gate: ScriptedGate, executed: list[str]) -> Any:
    """与 create_deep_agent 同序：Patch（核心栈）→ Suspension → Permission。

    自动模式：danger 是外部副作用（P1 没有判断器 → 请示），write 是工作区写入（放行）。
    """

    def danger(path: str) -> str:
        executed.append(path)
        return f"deleted {path}"

    def write(path: str) -> str:
        executed.append(f"write:{path}")
        return f"wrote {path}"

    tools = [
        StructuredTool.from_function(func=danger, name="danger", description="d"),
        StructuredTool.from_function(func=write, name="write", description="w"),
    ]
    return create_agent(
        model,
        tools=tools,
        middleware=[
            PatchToolCallsMiddleware(),
            SuspensionMiddleware(),
            PermissionMiddleware(
                effects={"danger": ToolEffect.EXTERNAL, "write": ToolEffect.WRITE},
                default_mode="auto",
                gate=gate,
            ),
        ],
    )


async def _final(agent: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """和 runner 一样流式跑，取最后一帧 values 作为最终 state。

    ★ 不用 ainvoke：没有 messages 流时模型走同步 _generate，TurnModel 的那条路径只回
      文本、丢掉 tool_call —— 图根本不会进 tools，测的就不是真实路径。
    """
    final: dict[str, Any] = {}
    async for mode, chunk in agent.astream(payload, stream_mode=["messages", "values"]):
        if mode == "values":
            final = chunk
    return final


def _results(messages: list[Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            out.setdefault(m.tool_call_id, []).append(str(m.content))
    return out


#: 这些测试里的图没有 run_id，approval_id 只由 tool_call_id 决定（approval.run_id_of）
TOKEN = approval_id_for("", "c1")


async def test_a_pending_approval_stops_before_the_model_and_leaves_the_call_dangling() -> None:
    """段 1：拦下、不执行、不写结果，只登记 waiting_on；模型只被调用一次。"""
    executed: list[str] = []
    model = TurnModel(
        scripts=[
            [tool_call_chunk("danger", '{"path":"/tmp/x"}', "c1")],
            [AIMessageChunk(content="x")],
        ]
    )
    final = await _final(
        _agent(model, ScriptedGate("pending"), executed),
        {"messages": [HumanMessage(content="删掉 /tmp/x")]}
    )

    assert executed == []
    assert model.call_count == 1, "挂起之后不该再调模型（它会拿到悬空的调用 → 400）"
    assert _results(final["messages"]) == {}, "挂起时不该写任何 tool 消息"
    assert final["waiting_on"] == {"c1": _wait("approval", TOKEN)}
    assert "decisions" not in final, "decisions 只进不出"


async def test_resume_starts_at_tools_without_a_checkpointer() -> None:
    """★ 段 2：新建的图 + 消息原样 + waiting_on + decisions → 直接从 tools 开始。

    ★ 这条同时钉住与 PatchToolCallsMiddleware 的共存：c1 悬空，但在 waiting_on 里，
      Patch 不补 cancelled —— 否则执行后同一个调用有两份结果。
    ★ 决策来自 state，不再问 gate。
    """
    executed: list[str] = []
    gate = ScriptedGate("pending")
    model = TurnModel(scripts=[[AIMessageChunk(content="收尾：文件已删除。")]])
    final = await _final(
        _agent(model, gate, executed),
        {
            "messages": list(_DANGLING),
            "waiting_on": {"c1": _wait("approval", TOKEN)},
            "decisions": {TOKEN: {"status": "approved"}},
        }
    )

    assert executed == ["/tmp/x"], "被批准的工具没有执行 —— jump_to tools 失败"
    assert _results(final["messages"]) == {"c1": ["deleted /tmp/x"]}
    assert not final.get("waiting_on"), "执行后要从 waiting_on 里删掉，否则来回跳 tools"
    assert model.call_count == 1
    assert gate.asked == 0


async def test_resume_without_waiting_on_is_patched_as_cancelled() -> None:
    """只交消息、漏了 waiting_on：「在等」被当成「被放弃」，工具永远不执行。

    这正是两份依据必须一起交回的理由（demo/suspend-resume 实验 4）——
    这里钉住的是失败形态，提醒 server 侧的 _resume_state 不能省。
    """
    executed: list[str] = []
    model = TurnModel(scripts=[[AIMessageChunk(content="好的。")]])
    final = await _final(
        _agent(model, ScriptedGate("approved"), executed),
        {"messages": list(_DANGLING)}
    )

    assert executed == []
    assert "was cancelled" in _results(final["messages"])["c1"][0]


async def test_a_rejected_decision_does_not_execute() -> None:
    """拒绝走同一条续跑路径：照样跳到 tools，只是审批中间件交出「拒绝」。"""
    executed: list[str] = []
    model = TurnModel(scripts=[[AIMessageChunk(content="那就不删了。")]])
    final = await _final(
        _agent(model, ScriptedGate("pending"), executed),
        {
            "messages": list(_DANGLING),
            "waiting_on": {"c1": _wait("approval", TOKEN)},
            "decisions": {TOKEN: {"status": "rejected"}},
        }
    )

    assert executed == []
    assert _results(final["messages"]) == {"c1": [REJECTED_RESULT]}
    assert not final.get("waiting_on")


async def test_parallel_batch_only_runs_the_released_call() -> None:
    """★ 并行的一批工具里只有一个在等审批：续跑时只执行它，已完成的兄弟调用不重跑。

    跳到 tools 节点时 LangChain 交给它的是整个 state，ToolNode 会把最后一条 AIMessage 的
    **所有** tool_call 再执行一遍 —— 兄弟调用的副作用发生两次，还多出一份 tool_result，
    下一次模型调用被网关拒绝（each tool_use must have a single result）。
    2026-10-13 智能体评测冒烟撞到：write_file + execute 并行，execute 等审批。
    """
    executed: list[str] = []
    model = TurnModel(scripts=[[AIMessageChunk(content="都完成了。")]])
    history = [
        HumanMessage(content="写文件再运行"),
        AIMessage(content="", tool_calls=[_call("c0", "write"), _call("c1", "danger")]),
        ToolMessage(content="wrote /tmp/x", tool_call_id="c0", name="write", id="tm-c0"),
    ]
    final = await _final(
        _agent(model, ScriptedGate("pending"), executed),
        {
            "messages": history,
            "waiting_on": {"c1": _wait("approval", TOKEN)},
            "decisions": {TOKEN: {"status": "approved"}},
        }
    )

    assert executed == ["/tmp/x"], executed
    assert _results(final["messages"]) == {"c0": ["wrote /tmp/x"], "c1": ["deleted /tmp/x"]}
    assert model.call_count == 1


def test_the_graph_always_has_a_tools_node() -> None:
    """★ `can_jump_to=["tools"]` 的前提：装出来的图里 tools 节点必然存在。

    不存在的话 LangGraph 在**编译时**就报 "found unknown target 'tools'" ——
    而那句错误离根因（某个 agent 一个工具都没配）很远，且只在跑那个 agent 时
    才出现。

    生产路径安全的理由是 create_deep_agent 总会装内置工具（write_todos 等），
    所以即使 spec 的 tool_names 是空的，tools 节点也在。这条测试钉住它：
    将来 kernel 改成「没勾工具就不装 tools 节点」时先在这里红。
    """
    from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec

    from .graphs import build_graph

    spec = AgentSpec(
        slug="bare",
        name="光板",
        system_prompt="x",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=(),  # 一个工具都没勾
        limits=LimitSpec(),
    )
    graph = build_graph(spec, TurnModel(scripts=[[AIMessageChunk(content="hi")]]))
    assert "tools" in graph.get_graph().nodes
