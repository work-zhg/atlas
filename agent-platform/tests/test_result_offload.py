"""大结果外置只有一处（code-native-runtime-context.html · ResultOffloader）。

单条、整批、超长用户输入用同一套规则：有存储就写全文、留头尾预览和路径；没有存储（模型读不回来）
就截断并说明。外置挂在上下文流水线上，不再挂在文件中间件上 —— 没勾文件工具的智能体也受保护。
"""

from __future__ import annotations

from typing import Any

import pytest
from atlas_engine.contracts import WriteResult
from atlas_engine.kernel.context import ContextMiddleware, ResultOffloader
from atlas_engine.kernel.context.offload import OFFLOAD_DIR
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

BIG = "\n".join(f"第 {i} 行：" + "数据" * 50 for i in range(2_000))  # ≈ 230k 字符


class Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.files: dict[str, str] = {}
        self._fail = fail

    async def awrite(self, path: str, content: str) -> WriteResult:
        if self._fail:
            return WriteResult(error="denied")
        self.files[path] = content
        return WriteResult(path=path)


def _result(content: str = BIG, call_id: str = "c1", name: str = "execute") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name=name, id=f"t-{call_id}")


async def test_a_large_result_is_written_whole_and_previewed() -> None:
    store = Store()
    out = await ResultOffloader(store=store).on_result(_result(), tool_name="execute")
    path = f"{OFFLOAD_DIR}/c1"
    assert store.files == {path: BIG}
    assert isinstance(out, ToolMessage) and out.id == "t-c1" and out.tool_call_id == "c1"
    assert path in out.content and "read_file" in out.content
    assert "第 0 行" in out.content and "第 1999 行" in out.content  # 头尾预览
    assert len(out.content) < 20_000


async def test_the_caller_decides_where_offloads_go() -> None:
    """外置目录由调用方给：消息里给模型的就是那里的路径。"""
    store = Store()
    offloader = ResultOffloader(
        store=store, offload_dir="/data/r/workspace/.atlas/t/large_tool_results"
    )
    out = await offloader.on_result(_result(), tool_name="execute")
    path = "/data/r/workspace/.atlas/t/large_tool_results/c1"
    assert list(store.files) == [path] and path in out.content


async def test_small_results_and_file_tools_are_left_alone() -> None:
    store = Store()
    offloader = ResultOffloader(store=store)
    small = _result("ok")
    assert await offloader.on_result(small, tool_name="execute") is small
    # read_file 自带分页：外置了模型只会再用 read_file 读一遍
    big_read = _result(name="read_file")
    assert await offloader.on_result(big_read, tool_name="read_file") is big_read
    assert store.files == {}


@pytest.mark.parametrize("store", [None, Store(fail=True)])
async def test_without_a_readable_store_the_result_is_clipped(store: Any) -> None:
    """没勾文件工具（读不回来）或写失败：截断并说明，而不是原样塞进上下文。"""
    out = await ResultOffloader(store=store, single_limit_tokens=4_000).on_result(
        _result(), tool_name="execute"
    )
    assert isinstance(out, ToolMessage)
    assert "已截断" in out.content and "没能保存完整内容" in out.content
    assert len(out.content) < 12_000
    assert out.content.count("第 0 行") == 1 and "第 1999 行" in out.content


async def test_results_inside_a_command_are_offloaded_too() -> None:
    store = Store()
    command = Command(update={"messages": [_result(call_id="c2")], "todos": []})
    out = await ResultOffloader(store=store).on_result(command, tool_name="some_tool")
    assert isinstance(out, Command)
    assert out.update["todos"] == [] and f"{OFFLOAD_DIR}/c2" in store.files
    assert "已外置" in out.update["messages"][0].content


async def test_batch_cap_offloads_the_largest_first() -> None:
    store = Store()
    batch = [
        AIMessage(content="", id="a", tool_calls=[
            {"id": "s", "name": "x", "args": {}}, {"id": "l", "name": "x", "args": {}}]),
        ToolMessage(content="小" * 1_000, tool_call_id="s", name="x", id="ts"),
        ToolMessage(content="大" * 50_000, tool_call_id="l", name="x", id="tl"),
    ]  # fmt: skip
    out = await ResultOffloader(store=store, batch_cap_tokens=5_000).on_batch(batch)
    assert [m.id for m in out] == ["tl"] and list(store.files) == [f"{OFFLOAD_DIR}/l"]
    assert "本批工具结果总量过大" in out[0].content


async def test_an_oversized_input_is_offloaded_once() -> None:
    store = Store()
    offloader = ResultOffloader(store=store, input_limit_tokens=1_000)
    message = HumanMessage(content=BIG, id="h1")
    out = await offloader.on_input([message])
    assert out is not None and out.id == "h1" and len(store.files) == 1
    assert next(iter(store.files.values())) == BIG
    # 打过标记的不再外置一次
    assert await offloader.on_input([out]) is None


async def test_the_pipeline_offloads_every_tool_result() -> None:
    """外置在上下文流水线的 wrap_tool_call 上：包在工具外面，任何工具的结果都经过它。"""
    store = Store()
    middleware = ContextMiddleware(offloader=ResultOffloader(store=store))

    class Request:
        def __init__(self) -> None:
            self.tool_call = {"name": "mcp_tool", "id": "c9", "args": {}}

    async def handler(_req: Any) -> ToolMessage:
        return _result(call_id="c9", name="mcp_tool")

    out = await middleware.awrap_tool_call(Request(), handler)
    assert f"{OFFLOAD_DIR}/c9" in store.files and "已外置" in out.content


def test_stages_that_need_levels_need_a_budget() -> None:
    from atlas_engine.kernel.context import ResultClearing

    with pytest.raises(ValueError, match="ContextBudget"):
        ContextMiddleware(clearing=ResultClearing())


def test_the_todo_reminder_comes_from_the_registry() -> None:
    """待办提醒的内容由工具面的 TodoList 给，勾了 write_todos 才有。"""
    from atlas_engine.runtime.tool_registry import reminders_for

    assert reminders_for(set()) == []
    (source,) = reminders_for({"write_todos"})
    todos = [{"content": "写测试", "status": "in_progress"}]
    steps = [AIMessage(content=f"step{i}") for i in range(8)]
    assert source({"todos": todos, "messages": steps})[0][0] == "todo"
    assert source({"todos": todos, "messages": steps[:2]}) == []
