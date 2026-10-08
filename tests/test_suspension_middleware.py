"""挂起的图内支点（S6）—— SuspensionMiddleware 的三条判据。

一轮可以分多段执行，而整件事在图里只需要三条判据（全部只看 messages）：

    最后一批里有「已放行」占位   → 删掉它 + jump_to "tools"（去执行那个工具）
    最后一批里有挂起哨兵         → jump_to "end"（本段到此为止）
    两者都没有                   → 正常流向 model

★ 判据是「已放行占位」而**不是**「tool_call 悬空」。后者是初版设计，真机验证
  时发现它与 kernel 的 PatchToolCallsMiddleware 直接冲突 —— 那个中间件在图
  入口给一切悬空的 tool_call 补上「was cancelled」，而它排在核心栈里、顺序
  无法调整。于是续跑段永远跳不到 tools，模型重新发一次调用又挂起：死循环。
  见 doc/detail/suspension.html §12 修正记录 10。

★ 委派与审批是同一件事，差别只在续跑时怎么处理那条哨兵（换内容 vs 删整条）。
  这个中间件因此不认识它们，只认 marker（doc/detail/suspension.html §02）。

★ 「不用 checkpointer」这条论证的实验证据就在这里：`jump_to "tools"` 能让图
  从工具节点开始执行，而输入只有 messages。
"""

from __future__ import annotations

from typing import Any

from atlas_engine.contracts import delegation_pending, released_marker, suspend_marker
from atlas_engine.kernel.middleware.suspension import SuspensionMiddleware
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from .fakes import TurnModel


def _call(call_id: str, name: str = "danger") -> dict[str, Any]:
    return {"type": "tool_call", "id": call_id, "name": name, "args": {"path": "/tmp/x"}}


def _decide(messages: list[Any]) -> dict[str, Any] | None:
    return SuspensionMiddleware().before_model({"messages": messages}, None)


# ──────────────────────────────────────────────── 判据本身


def test_a_pending_marker_jumps_to_end() -> None:
    assert _decide(
        [
            AIMessage(content="", tool_calls=[_call("c1", "task")]),
            ToolMessage(content=delegation_pending("sub-1"), tool_call_id="c1"),
        ]
    ) == {"jump_to": "end"}


def test_an_approval_marker_jumps_to_end_too() -> None:
    """★ 中间件不区分 reason —— 委派和审批走同一条跳出路径。"""
    assert _decide(
        [
            AIMessage(content="", tool_calls=[_call("c1")]),
            ToolMessage(content=suspend_marker("approval", "ap-1"), tool_call_id="c1"),
        ]
    ) == {"jump_to": "end"}


def test_a_released_placeholder_jumps_to_tools() -> None:
    """★ 审批续跑的入口：占位说「批了，去执行」。"""
    released = ToolMessage(content=released_marker("ap-1"), tool_call_id="c1", id="m1")
    out = _decide([AIMessage(content="", tool_calls=[_call("c1")]), released])
    assert out is not None
    assert out["jump_to"] == "tools"
    # 占位要被删掉 —— 否则 tools 跑完同一个 tool_call 会有两条结果
    assert [m.id for m in out["messages"]] == ["m1"]


def test_a_dangling_tool_call_alone_does_not_jump() -> None:
    """★ 悬空**不再**是判据。

    它曾经是（初版设计），而 PatchToolCallsMiddleware 会在图入口把悬空补掉，
    于是这条判据永远不成立。改用显式占位之后，悬空只是「上一段没收干净」，
    交给 patch 去补一句 cancelled 即可。
    """
    assert (
        _decide([HumanMessage(content="删掉它"), AIMessage(content="", tool_calls=[_call("c1")])])
        is None
    )


def test_a_completed_batch_flows_to_the_model() -> None:
    assert (
        _decide(
            [
                AIMessage(content="", tool_calls=[_call("c1")]),
                ToolMessage(content="ok", tool_call_id="c1"),
            ]
        )
        is None
    )


def test_released_wins_over_a_pending_marker() -> None:
    """一段里同时有两者时 **tools 优先**。

    先把已放行的工具跑掉；那条委派的哨兵下一轮 before_model 还会看到，到时候
    再挂起。反过来的话已批准的工具永远不会被执行。
    """
    out = _decide(
        [
            AIMessage(content="", tool_calls=[_call("c1", "task"), _call("c2")]),
            ToolMessage(content=delegation_pending("sub-1"), tool_call_id="c1"),
            ToolMessage(content=released_marker("ap-1"), tool_call_id="c2", id="m2"),
        ]
    )
    assert out is not None and out["jump_to"] == "tools"


def test_an_old_marker_deeper_in_history_is_ignored() -> None:
    """★ 只看**最后一批**。

    更早的哨兵属于已经续跑过的段（那时它已被换成真实结论）。真出现在历史里
    也只说明上一段没清理干净 —— 再跳出一次只会让 run 卡在原地。
    """
    assert (
        _decide(
            [
                AIMessage(content="", tool_calls=[_call("c1", "task")]),
                ToolMessage(content=delegation_pending("sub-1"), tool_call_id="c1"),
                AIMessage(content="继续", tool_calls=[_call("c2")]),
                ToolMessage(content="ok", tool_call_id="c2"),
            ]
        )
        is None
    )


def test_an_earlier_sealed_batch_is_not_re_executed() -> None:
    """更早的悬空调用不该被重新执行 —— 那是重复副作用。

    to_storage 会给中断的旧批次补上 is_error 的结果，所以历史里本不该有；
    但判据自己也要守住这条，不能靠上游一定干净。
    """
    assert (
        _decide(
            [
                AIMessage(content="", tool_calls=[_call("old")]),  # 没有结果
                AIMessage(content="", tool_calls=[_call("new")]),
                ToolMessage(content="ok", tool_call_id="new"),
            ]
        )
        is None
    )


def test_garbage_content_is_not_mistaken_for_a_marker() -> None:
    """判错方向要选安全的那一边。

    把普通结果当 marker 的后果是 run 永久停在 suspended（静默、难查）；
    反过来只是模型看到一串奇怪的字符（可见、可查）。
    """
    for content in (
        "__ATLAS_SUSPENDED__",  # 缺字段
        "__ATLAS_SUSPENDED__:delegation",  # 缺 token
        "__ATLAS_SUSPENDED__:bogus:x",  # reason 不认识
        "__ATLAS_SUSPENDED__:delegation:",  # token 空
        "前缀不在开头 __ATLAS_SUSPENDED__:delegation:x",
    ):
        assert (
            _decide(
                [
                    AIMessage(content="", tool_calls=[_call("c1")]),
                    ToolMessage(content=content, tool_call_id="c1"),
                ]
            )
            is None
        ), content


# ──────────────────────────────────────────────── 图内的真实行为


async def test_the_graph_really_starts_at_tools_without_a_checkpointer() -> None:
    """★ 「不引入 checkpointer」的实验证据。

    输入只有 messages（InputAgentState 就这一个字段 —— `jump_to` 标着
    PrivateStateAttr，从图的输入 schema 里排除，传进去会被直接丢弃）。
    判据来自 state 本身，middleware 在图内自己决定，于是不需要任何外部
    状态存储就能「从工具节点继续」。

    ★ 这条同时钉住与 PatchToolCallsMiddleware 的共存：输入序列是**配对完整**
      的（占位就是那个结果），所以 patch 在图入口不插手；删除推到 before_model
      才做，那时它已经跑完。
    """
    executed: list[str] = []

    def danger(path: str) -> str:
        executed.append(path)
        return f"deleted {path}"

    tool = StructuredTool.from_function(func=danger, name="danger", description="d")
    model = TurnModel(scripts=[[AIMessageChunk(content="收尾：文件已删除。")]])
    agent = create_agent(model, tools=[tool], middleware=[SuspensionMiddleware()])

    # 审批放行后的历史形态：配对完整，结果是一条「已放行」占位
    history = [
        HumanMessage(content="删掉 /tmp/x"),
        AIMessage(content="我来删", tool_calls=[_call("c1")]),
        ToolMessage(content=released_marker("ap-1"), tool_call_id="c1", name="danger"),
    ]

    nodes: list[str] = []
    async for _ns, _mode, chunk in agent.astream(
        {"messages": history}, stream_mode=["updates"], subgraphs=True
    ):
        if isinstance(chunk, dict):
            nodes.extend(chunk)

    assert executed == ["/tmp/x"], "工具没被执行 —— jump_to tools 失败"
    assert "tools" in nodes, nodes
    # tools 跑完回到 before_model，这次没有悬空的了 → 正常流向 model
    assert nodes.index("tools") < nodes.index("model"), nodes


async def test_a_marker_stops_the_graph_before_the_model() -> None:
    """哨兵之后模型不再被调用 —— 它永远看不到 marker。"""
    noop = StructuredTool.from_function(func=lambda: "ok", name="noop", description="d")
    model = TurnModel(scripts=[[AIMessageChunk(content="不该被调用")]])
    agent = create_agent(model, tools=[noop], middleware=[SuspensionMiddleware()])

    history = [
        HumanMessage(content="派活"),
        AIMessage(content="", tool_calls=[_call("c1", "task")]),
        ToolMessage(content=delegation_pending("sub-1"), tool_call_id="c1"),
    ]
    async for _ns, _mode, _chunk in agent.astream(
        {"messages": history}, stream_mode=["updates"], subgraphs=True
    ):
        pass

    assert model.call_count == 0, "哨兵之后不该有模型调用"


def test_the_graph_always_has_a_tools_node() -> None:
    """★ `can_jump_to=["tools"]` 的前提：装出来的图里 tools 节点必然存在。

    不存在的话 LangGraph 在**编译时**就报 "found unknown target 'tools'" ——
    而那句错误离根因（某个 agent 一个工具都没配）很远，且只在跑那个 agent 时
    才出现。

    生产路径安全的理由是 create_deep_agent 总会装内置工具（write_todos 等），
    所以即使 spec 的 tool_names 是空的，tools 节点也在。这条测试钉住它：
    将来 kernel 改成「没勾工具就不装 tools 节点」时先在这里红。
    """
    from atlas_server.domain.spec import AgentSpec, LimitSpec, ModelSpec

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
