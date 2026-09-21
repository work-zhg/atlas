"""acp 子智能体（acp 详设 §12 步骤 6）。

native 主 agent 委派给一个 **kind=acp** 的子智能体：「让 Claude Code 去改
这三个文件」。反过来不行 —— acp agent 不能委派（见最后一条断言）。

设计说这一步「机制上零新增」。落地时发现**不成立**，两处要补：
  · build_graph 只被 NativeRuntime 调用 ⇒ acp agent 永远拿不到 task 工具，
    它配的 subagents 会被静默忽略（§13.2 不允许）
  · spec_for_subagent 原先硬编码 kind=parent.kind ⇒ native 父只能产出
    native 子，「acp 子智能体」根本造不出来

补完之后**委派链路本身确实一行未改** —— 子会话也是 thread，
AcpRuntime 对它原样生效，这才是 §02 那条对称性的兑现。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_server.acp.pods import PodEndpoint
from atlas_server.acp.runtime import AcpRuntime
from atlas_server.config import get_settings
from atlas_server.db.models import Run, Thread
from atlas_server.db.session import get_sessionmaker
from atlas_server.domain.events import EventType
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from atlas_bridge.adapter import AdapterProcess
from atlas_bridge.ws import BridgeServer

from .fakes import TurnModel, tool_call_chunk

pytestmark = pytest.mark.usefixtures("clean_db")

TOKEN = "subagent-pod-token"
PARENT_MODEL = "claude-opus-5"
_FAKE = str(Path(__file__).parent / "fake_adapter.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class BridgeFarm:
    """PodProvider 的假实现：**按 thread 懒起一个 bridge**。

    ★ 这正是 PodManager 的真实形态（一个会话一个 Pod，按需创建、幂等复用）。
      子会话的 id 到委派那一刻才存在，所以不能像前几步那样预先绑定 ——
      这条测试也因此顺带验证了「子会话的 Pod 是独立的一个」。
    """

    def __init__(self, **script: str) -> None:
        self._script = script
        self._bridges: dict[str, tuple[PodEndpoint, BridgeServer, AdapterProcess, asyncio.Task]] = {}

    async def ensure(self, thread: Any, cli: Any) -> PodEndpoint:
        key = str(thread.id)
        if key in self._bridges:
            return self._bridges[key][0]

        port = _free_port()
        server: BridgeServer | None = None

        async def on_notification(frame: dict[str, Any]) -> None:
            assert server is not None
            await server.on_adapter_notification(frame)

        async def on_request(frame: dict[str, Any]) -> Any:
            assert server is not None
            return await server.on_adapter_request(frame)

        adapter = AdapterProcess(
            [sys.executable, _FAKE],
            on_notification=on_notification,
            on_request=on_request,
            env={**os.environ, **self._script},
        )
        # 会话绑定：bridge 只接受针对**本 Pod 所属会话**的指令
        server = BridgeServer(adapter, token=TOKEN, thread_id=key, request_timeout_s=30)
        await adapter.start()
        task = asyncio.create_task(server.serve_forever("127.0.0.1", port))
        await asyncio.sleep(0.15)

        endpoint = PodEndpoint(url=f"ws://127.0.0.1:{port}", token=TOKEN)
        self._bridges[key] = (endpoint, server, adapter, task)
        return endpoint

    @property
    def pod_count(self) -> int:
        return len(self._bridges)

    async def aclose(self) -> None:
        for _endpoint, _server, adapter, task in self._bridges.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await adapter.stop()


@contextlib.asynccontextmanager
async def stack(parent: TurnModel, **script: str) -> AsyncIterator[tuple[httpx.AsyncClient, BridgeFarm]]:
    farm = BridgeFarm(**script)
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: parent,
        acp_runtime=AcpRuntime(get_sessionmaker(), get_settings(), farm),
    )
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield client, farm
    finally:
        await client.aclose()
        await farm.aclose()


async def new_thread(client: httpx.AsyncClient, agent_id: str) -> str:
    """建会话时**给标题**。

    ★ 不给的话 title_source='pending'，首轮结束会调 titler —— 而测试里
      titler 与主 agent 共用同一个 TurnModel，它会吃掉下一段脚本，
      第二次委派于是静默消失。踩过一次：表现是「只有一个子 run」，
      而真实链路完全正常。
    """
    resp = await client.post("/v1/threads", json={"agent_id": agent_id, "title": "测试会话"})
    return resp.json()["id"]


async def make_agent(client: httpx.AsyncClient, *, slug: str = "lead") -> str:
    """native 主 agent + 一个 acp 子智能体。"""
    resp = await client.post(
        "/v1/agents",
        json={
            "slug": slug,
            "name": "主管",
            "spec": {
                "system_prompt": "你会把编码任务交给 coder。",
                "model": {"model": PARENT_MODEL},
                "tool_names": ["task"],
                "subagents": [
                    {
                        "name": "coder",
                        "description": "用 Claude Code 改代码",
                        "system_prompt": "改代码",
                        "model": {"model": PARENT_MODEL},
                        "kind": "acp",
                        "cli": {
                            "cli_type": "claude-code",
                            "adapter": "node acp",
                            "image": "atlas-acp:1",
                        },
                    }
                ],
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _delegating(n: int = 1) -> TurnModel:
    """发 n 次委派，每次一轮。"""
    scripts: list[list[AIMessageChunk]] = []
    for i in range(n):
        scripts.append(
            [
                tool_call_chunk(
                    "task",
                    json.dumps({"description": f"第 {i + 1} 步", "subagent_type": "coder"}),
                    f"c{i}",
                )
            ]
        )
        scripts.append([AIMessageChunk(content=f"第 {i + 1} 步完成")])
    return TurnModel(scripts=scripts)


async def run_turn(client: httpx.AsyncClient, thread_id: str, text: str) -> dict:
    accepted = await client.post(
        f"/v1/threads/{thread_id}/runs", json={"content": [{"type": "text", "text": text}]}
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 超时未结束")


async def sub_threads(parent_id: str) -> list[Thread]:
    async with get_sessionmaker()() as session:
        stmt = (
            select(Thread)
            .where(Thread.parent_thread_id == UUID(parent_id))
            .order_by(Thread.created_at, Thread.id)
        )
        return list((await session.execute(stmt)).scalars())


async def runs_of(thread_id) -> list[Run]:
    async with get_sessionmaker()() as session:
        return list(
            (await session.execute(select(Run).where(Run.thread_id == thread_id))).scalars()
        )


# ──────────────────────────────────────────────── 委派链路未改


async def test_native_parent_delegates_to_an_acp_subagent() -> None:
    """★ 委派链路一行未改：子会话是 thread，AcpRuntime 对它原样生效。

    子会话、子 run、parent_run_id、会话锁全都走与 native 子智能体完全
    相同的路径（subagent §02 的对称性）。
    """
    async with stack(_delegating()) as (client, farm):
        agent_id = await make_agent(client)
        thread = await new_thread(client, agent_id)

        result = await run_turn(client, thread, "改三个文件")
        assert result["status"] == "succeeded", result

        subs = await sub_threads(thread)
        assert len(subs) == 1
        assert subs[0].subagent_name == "coder"
        # ★ 子会话挂**父的**工作区 —— Pod 的 subPath 由此派生（cluster 模板）
        assert subs[0].workspace_thread_id == UUID(thread)

        child_runs = await runs_of(subs[0].id)
        assert len(child_runs) == 1
        assert child_runs[0].parent_run_id == UUID(result["id"])
        assert child_runs[0].status == "succeeded"


async def test_the_subagent_gets_its_own_pod() -> None:
    """一个会话一个 Pod —— 子会话是独立的会话，所以是独立的 Pod。

    父是 native（进程内跑），所以这里只应有**子会话那一个** Pod。
    """
    async with stack(_delegating()) as (client, farm):
        agent_id = await make_agent(client)
        thread = await new_thread(client, agent_id)
        await run_turn(client, thread, "去")
        assert farm.pod_count == 1


async def test_parent_only_sees_the_conclusion() -> None:
    """主线程不含子智能体的中间过程 —— 与 native 子智能体同款语义。"""
    from atlas_server.db.models import Message

    async with stack(_delegating()) as (client, _farm):
        agent_id = await make_agent(client)
        thread = await new_thread(client, agent_id)
        await run_turn(client, thread, "去")

        async with get_sessionmaker()() as session:
            rows = list(
                (
                    await session.execute(select(Message).where(Message.thread_id == UUID(thread)))
                ).scalars()
            )
        blob = json.dumps([r.content for r in rows], ensure_ascii=False)
        assert "先看代码" not in blob  # CLI 的思考
        assert "第 1 步完成" in blob  # 主 agent 自己的话


# ──────────────────────────────────────────────── 恢复（本步骤的验收点）


async def test_second_delegation_resumes_the_cli_session() -> None:
    """★ 验收点一：第二次委派时 CLI 记得上次改过什么。

    external_session_id 落在**子会话**的 thread 行上，第二次委派命中同一个
    子会话 → caps 允许 → 走 session/load 而不是 session/new。
    """
    async with stack(_delegating(2)) as (client, farm):
        agent_id = await make_agent(client)
        thread = await new_thread(client, agent_id)

        await run_turn(client, thread, "第一步")
        subs = await sub_threads(thread)
        assert len(subs) == 1
        async with get_sessionmaker()() as session:
            sub = await session.get(Thread, subs[0].id)
            assert sub is not None
            assert sub.external_session_id == "fake-session-1"

        await run_turn(client, thread, "第二步")

        # 仍然只有一个子会话、一个 Pod，两个子 run
        subs = await sub_threads(thread)
        assert len(subs) == 1
        assert farm.pod_count == 1
        assert len(await runs_of(subs[0].id)) == 2


async def test_a_cli_that_cannot_resume_reports_lost() -> None:
    """★ 验收点二：不支持 resume 的 CLI 标 lost，**绝不静默新建**。

    静默新建的表现是：用户以为接着上次继续，CLI 实际从零开始 —— 它会重新
    读一遍代码、重新问一遍已经回答过的问题，而事件流上看不出任何异常。
    这是最坏的失败形态（Subagent §04）。
    """
    from atlas_server.db.models import RunEvent

    async with stack(_delegating(2), ACP_FAKE_NO_RESUME="1") as (client, _farm):
        agent_id = await make_agent(client)
        thread = await new_thread(client, agent_id)

        await run_turn(client, thread, "第一步")
        await run_turn(client, thread, "第二步")

        subs = await sub_threads(thread)
        second_run = sorted(await runs_of(subs[0].id), key=lambda r: r.created_at)[-1]

        async with get_sessionmaker()() as session:
            kinds = [
                row.type
                for row in (
                    await session.execute(
                        select(RunEvent).where(RunEvent.run_id == second_run.id)
                    )
                ).scalars()
            ]
        assert EventType.SESSION_LOST in kinds, "上下文丢了却没让用户看见"


# ──────────────────────────────────────────────── 反向不成立


def test_an_acp_agent_cannot_declare_subagents() -> None:
    """★ acp agent **不能委派**，配了必须报错。

    平台的 task 工具装在 LangGraph 图上，而 acp 的一轮根本不建图（工具面
    由 CLI 自带）。静默忽略这份配置就是「配了却不生效」—— §13.2 不允许。
    这是落地时发现设计「零新增」不成立的两处之一。
    """
    from atlas_engine.contracts import InvalidSpec
    from atlas_server.schemas.agent import AgentSpecIn

    spec = AgentSpecIn.model_validate(
        {
            "model": {"model": PARENT_MODEL},
            "kind": "acp",
            "cli": {"cli_type": "claude-code"},
            "subagents": [{"name": "x", "model": {"model": PARENT_MODEL}}],
        }
    ).to_engine(slug="a", name="A")

    with pytest.raises(InvalidSpec, match="不能配置子智能体"):
        spec.validate()


def test_an_acp_subagent_must_carry_cli_config() -> None:
    """kind=acp 却没有镜像 = 建不出 Pod。在保存 agent 时就拦住，
    而不是等某次委派才发现。"""
    from atlas_engine.contracts import InvalidSpec
    from atlas_server.schemas.agent import AgentSpecIn

    spec = AgentSpecIn.model_validate(
        {
            "model": {"model": PARENT_MODEL},
            "tool_names": ["task"],
            "subagents": [{"name": "coder", "model": {"model": PARENT_MODEL}, "kind": "acp"}],
        }
    ).to_engine(slug="a", name="A")

    with pytest.raises(InvalidSpec, match="缺少 cli"):
        spec.validate()
