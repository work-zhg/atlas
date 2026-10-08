"""装配期失败也要产出 run.started → run.failed。

此前装配异常（MCP 缺工具、web_search 缺 key、记忆未开启……）直接冒泡到
执行器兜底：run 落成「执行器内部错误」，**一个事件都不发**。SSE 上既无开始
也无结束，而能直接指出病根的原因只留在服务端日志里。

不连库：NativeRuntime 只需要一个会抛异常的 assembly。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from atlas_engine.contracts import InvalidSpec
from atlas_server.domain.events import EventType
from atlas_server.domain.spec import AgentSpec, ModelSpec
from atlas_server.executor.runtime import NativeRuntime
from atlas_server.memory.tool import MemoryUnavailable
from atlas_server.providers.mcp import MissingTool

SPEC = AgentSpec(
    slug="a",
    name="A",
    system_prompt="",
    model=ModelSpec(model="claude-sonnet-5"),
    tool_names=("mcp:github:search",),
)


class _FailingAssembly:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def build(self, *_a: Any, **_kw: Any) -> Any:
        raise self._exc


async def _turn(exc: BaseException, *, start_seq: int = 0) -> list[Any]:
    runtime = NativeRuntime(assembly=_FailingAssembly(exc))  # type: ignore[arg-type]
    prepared = SimpleNamespace(spec=SPEC, start_seq=start_seq, base_depth=0)
    return [
        e
        async for e in runtime.run_turn(
            prepared,  # type: ignore[arg-type]
            run_id=uuid.uuid4(),
            redis=None,  # type: ignore[arg-type]
            relay=None,  # type: ignore[arg-type]
        )
    ]


async def test_capability_error_becomes_visible_run_failed() -> None:
    """★ 用户看到的是具体原因，不是「执行器内部错误」。"""
    events = await _turn(MissingTool("MCP server 'github' 上不存在工具：['search']"))

    assert [e.type for e in events] == [EventType.RUN_STARTED, EventType.RUN_FAILED]
    failed = events[-1]
    assert failed.ends_segment
    assert failed.data["error_kind"] == "capability_unavailable"
    assert "不存在工具" in failed.data["message"]
    assert failed.data["retryable"] is False
    # run.started 与正常路径同形 —— 前端不用区分这是哪条路
    assert events[0].data["agent_slug"] == "a"
    assert events[0].data["tools"] == ["mcp:github:search"]


async def test_existing_capability_errors_share_the_path() -> None:
    """记忆未开启此前也走「内部错误」那条路。"""
    events = await _turn(MemoryUnavailable("勾选了 search_memory 但服务端未开启记忆"))
    assert events[-1].data["error_kind"] == "capability_unavailable"
    assert "未开启记忆" in events[-1].data["message"]


async def test_engine_error_keeps_its_kind() -> None:
    events = await _turn(InvalidSpec("配置不合法", field="x"))
    assert events[-1].data["error_kind"] == "invalid_spec"
    assert events[-1].data["field"] == "x"


async def test_unexpected_error_does_not_leak_details() -> None:
    """★ 真正的 bug：细节只进日志，用户看到的是既有的兜底文案。"""
    events = await _turn(KeyError("db_password=hunter2"))
    assert events[-1].data["error_kind"] == "internal_error"
    assert events[-1].data["message"] == "执行器内部错误"
    assert "hunter2" not in str(events[-1].data)


async def test_seq_continues_from_previous_segment() -> None:
    """★ 续跑段装配失败时，seq 接着上一段往下数（契约规则 2：跨段无空洞）。"""
    events = await _turn(MissingTool("x"), start_seq=7)
    assert [e.seq for e in events] == [8, 9]


async def test_cancellation_is_not_turned_into_failure() -> None:
    """CancelledError 是 BaseException，不能被当成装配失败吞掉。"""
    with pytest.raises(asyncio.CancelledError):
        await _turn(asyncio.CancelledError())
