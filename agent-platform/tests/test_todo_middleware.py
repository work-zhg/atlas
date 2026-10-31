"""待办中间件（kernel/middleware/todo.py）与上游唯一的不同：todos 能从输入交回图。

★ 走真实的 build_graph（spec 勾了 write_todos → tool_registry 装 kernel 这份），
  所以装配点接错（又装回上游那份）也会在这里红。
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessageChunk, HumanMessage

from .fakes import TurnModel, tool_call_chunk
from .graphs import build_graph

OLD = [{"content": "查资料", "status": "completed"}, {"content": "写报告", "status": "in_progress"}]
NEW = [{"content": "查资料", "status": "completed"}, {"content": "写报告", "status": "completed"}]


def _resumed() -> dict:
    """续跑那一段的输入：消息 + 上一段的计划。"""
    return {"messages": [HumanMessage(content="写份报告")], "todos": OLD}


def _spec() -> object:
    from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec

    return AgentSpec(
        slug="todo",
        name="待办",
        system_prompt="p",
        model=ModelSpec(model="claude-sonnet-5"),
        tool_names=("write_todos",),
        limits=LimitSpec(timeout_s=30),
    )


async def _final_state(graph: object, graph_input: dict) -> dict:
    """像 runner 那样流式驱动（带 messages 模式）—— TurnModel 只在流式调用时吐 tool_call。"""
    final: dict = {}
    async for mode, chunk in graph.astream(graph_input, stream_mode=["messages", "values"]):  # type: ignore[attr-defined]
        if mode == "values":
            final = chunk
    return final


def test_todos_is_part_of_the_graph_input_schema() -> None:
    """上游给 todos 标了 OmitFromInput，装回上游那份时这里先红。"""
    graph = build_graph(_spec(), TurnModel(scripts=[[AIMessageChunk(content="好")]]))
    assert "todos" in graph.get_input_jsonschema()["properties"]


async def test_todos_from_input_survive_when_the_model_does_not_rewrite_them() -> None:
    """续跑的形状：上一段的计划经输入交回，这一段模型没再调 write_todos，计划仍在。"""
    graph = build_graph(_spec(), TurnModel(scripts=[[AIMessageChunk(content="继续写")]]))

    final = await _final_state(graph, _resumed())

    assert final["todos"] == OLD


async def test_write_todos_replaces_the_list_passed_in() -> None:
    """交回的计划只是起点：模型调 write_todos 照常整表替换。"""
    args = json.dumps({"todos": NEW}, ensure_ascii=False)
    model = TurnModel(
        scripts=[[tool_call_chunk("write_todos", args, "c1")], [AIMessageChunk(content="写完了")]]
    )
    graph = build_graph(_spec(), model)

    final = await _final_state(graph, _resumed())

    assert final["todos"] == NEW
