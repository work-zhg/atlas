"""对真实 litellm 网关的集成测试 —— 默认跳过。

    ATLAS_LIVE_TESTS=1 uv run pytest tests/test_live_gateway.py -v

CI 不跑（无 key、会真实计费）。但本地改动 model_factory / compat 后必须跑一遍：
单测用假模型，覆盖不到"网关到底收不收这个参数"这一层，而那恰恰是踩过坑的地方。
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from atlas_agent.domain.events import EventType
from atlas_agent.domain.spec import AgentSpec, LimitSpec, ModelSpec
from atlas_agent.providers.llm.factory import build_chat_model

from tests.graphs import run_agent as run

pytestmark = pytest.mark.skipif(
    not os.getenv("ATLAS_LIVE_TESTS"),
    reason="需要 ATLAS_LIVE_TESTS=1 且有可用网关 key",
)

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")


def _creds() -> tuple[str, str]:
    base = os.getenv("LITELLM_BASE_URL", "https://apijp.techstz.com")
    key = os.environ["LITELLM_KEY"]
    return base, key


def _spec(model: str) -> AgentSpec:
    # 刻意不指定 effort / thinking —— 让 engine 按模型能力解析。
    # 写死 effort="low" 会在 haiku-4-5 上被正确拒绝（它不支持该参数）。
    return AgentSpec(
        slug="live",
        name="live",
        system_prompt="You are terse. Answer with the fewest words possible.",
        model=ModelSpec(model=model, max_output_tokens=256),
        limits=LimitSpec(timeout_s=120),
    )


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"])
async def test_live_stream_end_to_end(model: str) -> None:
    """★ 回归护栏：litellm 的 context_management 曾让流式整个崩掉（见 compat.py）。"""
    base, key = _creds()
    spec = _spec(model)
    chat = build_chat_model(spec.model, base_url=base, api_key=key)

    events = [
        e
        async for e in run(spec, run_id=RUN_ID, model=chat, input_content="Reply with exactly: OK")
    ]
    types = [e.type for e in events]

    assert types[0] == EventType.RUN_STARTED
    assert EventType.RUN_FAILED not in types, next(
        e.data for e in events if e.type == EventType.RUN_FAILED
    )
    assert types[-1] == EventType.RUN_FINISHED
    assert types.count(EventType.MESSAGE_DELTA) >= 1

    usage = next(e.data for e in events if e.type == EventType.USAGE_UPDATED)
    assert usage["output_tokens"] > 0


async def test_live_temperature_rejected_by_gateway() -> None:
    """确认 supports_temperature 门禁的依据仍然成立（网关行为若变，这里会亮）。"""
    base, key = _creds()
    from atlas_engine.contracts import InvalidSpec

    # engine 侧先拦下来
    with pytest.raises(InvalidSpec):
        build_chat_model(
            ModelSpec(model="claude-opus-5", temperature=0.2), base_url=base, api_key=key
        )


async def test_live_tools_produce_inspector_data() -> None:
    """★ P4 完成标准：Inspector 三个 Tab 有真数据。

    用真实模型跑一轮强制工具调用，断言三类事件都产出：
      todos.updated → 计划页 / tool.* → 工具页 / file.written → 文件页
    """
    base, key = _creds()
    spec = AgentSpec(
        slug="live-tools",
        name="工具测试",
        system_prompt="你是助手。先用 write_todos 列计划，再用 write_file 写文件。",
        model=ModelSpec(model="claude-sonnet-5", effort="low", max_output_tokens=2048),
        tool_names=("write_todos", "filesystem"),
        limits=LimitSpec(timeout_s=180),
    )
    chat = build_chat_model(spec.model, base_url=base, api_key=key)

    events = [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=chat,
            input_content="把 1+1 的结果写进 /result.txt。先列两条待办。",
        )
    ]
    types = [e.type for e in events]
    assert EventType.RUN_FAILED not in types, next(
        e.data for e in events if e.type == EventType.RUN_FAILED
    )

    assert EventType.TOOL_STARTED in types, "工具页无数据"
    assert EventType.TODOS_UPDATED in types, "计划页无数据"
    assert EventType.FILE_WRITTEN in types, "文件页无数据"
    assert types[-1] == EventType.RUN_FINISHED

    # todos 是全量快照（契约规则 1）
    todos = [e.data["todos"] for e in events if e.type is EventType.TODOS_UPDATED]
    assert all(isinstance(t, list) for t in todos)
    assert any(len(t) >= 2 for t in todos)

    written = [e.data["path"] for e in events if e.type is EventType.FILE_WRITTEN]
    assert "/result.txt" in written

    # seq 仍严格递增无空洞
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


async def test_live_mcp_web_search_via_gateway() -> None:
    """网页搜索的真实链路：模型 → MCP 工具 → MCP 网关 → Higress → serpapi。

    前提：MCP 网关在跑、serpapi 已在网关登记（Higress 上只认网关凭据），主体有授权、有 Key。
    ATLAS_LIVE_MCP_GATEWAY_URL（默认本机 8070）、ATLAS_LIVE_MCP_SUBJECT（主体的用户中心 uid）。
    """
    import uuid

    import httpx
    from atlas_agent.providers.mcp import CallContext, KeyAuth, McpSessions, NativeMcpTools
    from atlas_agent.providers.mcp.gateway import gateway_catalog, subject_access

    gateway = os.getenv("ATLAS_LIVE_MCP_GATEWAY_URL", "http://127.0.0.1:8070")
    subject = os.getenv("ATLAS_LIVE_MCP_SUBJECT")
    if not subject:
        pytest.skip("需要 ATLAS_LIVE_MCP_SUBJECT（有 serpapi 授权和 API Key 的主体）")
    try:
        httpx.get(f"{gateway}/health", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"MCP 网关不可达：{gateway}")

    base, key = _creds()
    spec = AgentSpec(
        slug="live-search",
        name="搜索宿主",
        system_prompt="需要查证最新事实时用搜索工具，回答带来源链接。",
        model=ModelSpec(model="claude-sonnet-5", effort="low", max_output_tokens=2048),
        tool_names=("mcp:serpapi:google_search",),
        limits=LimitSpec(timeout_s=180),
    )
    catalog = await gateway_catalog(gateway, ttl_s=0, redis=None)
    access = await subject_access(gateway, subject, ttl_s=0)
    sessions = McpSessions(auth=KeyAuth(access.key) if access.key else None)
    tools = await NativeMcpTools(catalog).tools_for(
        spec.tool_names,
        CallContext(run_id=RUN_ID, thread_id=uuid.uuid4(), user_id=uuid.UUID(subject)),
        reviews=catalog.reviews(),
        access=access,
        sessions=sessions,
    )
    assert tools, "serpapi 没装上：主体没有授权或没有 Key"
    chat = build_chat_model(spec.model, base_url=base, api_key=key)
    events = [
        e
        async for e in run(
            spec,
            run_id=RUN_ID,
            model=chat,
            input_content="查一下 Python 3.13 正式发布的日期，给出来源。",
            extra_tools=tools,
        )
    ]
    types = [e.type for e in events]
    assert EventType.RUN_FAILED not in types, next(
        e.data for e in events if e.type == EventType.RUN_FAILED
    )
    assert types[-1] == EventType.RUN_FINISHED
    searches = [
        e
        for e in events
        if e.type is EventType.TOOL_STARTED and e.data.get("name") == "serpapi__google_search"
    ]
    assert searches, "模型没有调用 serpapi__google_search"
    await sessions.aclose()
