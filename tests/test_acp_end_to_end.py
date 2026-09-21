"""acp 全链路（acp 详设 §12 步骤 4）。

spec.kind=acp 的 agent：POST /runs → AcpRuntime → bridge → 假 adapter →
update 流 → TraceEvent → SSE。

★ 验收标准就是那句话：**web 一行不改**。所以这里的断言不看「acp 跑通了」，
  看的是**产出的事件与 native 同形**：同一批 EventType、同一套 data 字段名。
  前端的 reducer 按字段名取值 —— 少一个字段就是「前端要加分支」。

不需要 K8s：bridge 在本进程起，PodProvider 是个指向它的假实现。
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
from atlas_server.db.session import get_sessionmaker
from atlas_server.domain.events import EventType
from atlas_server.executor.inprocess import InProcessExecutor
from atlas_server.main import create_app
from httpx import ASGITransport

from atlas_bridge.adapter import AdapterProcess
from atlas_bridge.ws import BridgeServer

pytestmark = pytest.mark.usefixtures("clean_db")

TOKEN = "e2e-pod-token"
_FAKE = str(Path(__file__).parent / "fake_adapter.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LocalPods:
    """PodProvider 的假实现 —— 指向本进程起的 bridge。

    步骤 5 的 PodManager 换掉它（K8s 模板 + 配对 Secret），AcpRuntime
    一行不改：它只认 (url, token)。
    """

    def __init__(self, port: int) -> None:
        self._endpoint = PodEndpoint(url=f"ws://127.0.0.1:{port}", token=TOKEN)
        self.calls = 0

    async def ensure(self, thread: Any, cli: Any) -> PodEndpoint:
        self.calls += 1
        return self._endpoint


@contextlib.asynccontextmanager
async def acp_stack(thread_id: str, **script: str) -> AsyncIterator[tuple[httpx.AsyncClient, LocalPods]]:
    """起 bridge + 假 adapter + 接了 AcpRuntime 的 app。"""
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
        env={**os.environ, **script},
    )
    server = BridgeServer(adapter, token=TOKEN, thread_id=thread_id, request_timeout_s=30)
    await adapter.start()
    serving = asyncio.create_task(server.serve_forever("127.0.0.1", port))
    await asyncio.sleep(0.15)

    pods = LocalPods(port)
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: None,  # acp 不用模型
        acp_runtime=AcpRuntime(get_sessionmaker(), get_settings(), pods),
    )
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client, pods
    finally:
        await client.aclose()
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        await adapter.stop()


async def setup_thread(slug: str = "cli-helper") -> str:
    """先建 agent 与会话 —— bridge 的会话绑定要拿真实 thread_id 才能起。

    ★ 用裸 app（不接 AcpRuntime）：建资源不需要执行环境，把它与「跑一轮」
      分开之后，acp_stack 只负责后者，agent 也就不会被建两次。
    """
    app = create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await make_acp_agent(client, slug=slug)
        return (await client.post("/v1/threads", json={"agent_id": agent_id})).json()["id"]


async def make_acp_agent(client: httpx.AsyncClient, *, slug: str = "cli-helper") -> str:
    resp = await client.post(
        "/v1/agents",
        json={
            "slug": slug,
            "name": "CLI 助理",
            "spec": {
                "system_prompt": "你是 CLI 助理",
                "model": {"model": "claude-sonnet-5"},
                "kind": "acp",
                "cli": {"cli_type": "claude-code", "adapter": "fake", "image": "fake:1"},
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def run_turn(client: httpx.AsyncClient, thread_id: str, text: str) -> dict:
    accepted = await client.post(
        f"/v1/threads/{thread_id}/runs", json={"content": [{"type": "text", "text": text}]}
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = asyncio.get_running_loop().time() + 25.0
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 超时未结束")


async def archived(run_id: str) -> list[dict[str, Any]]:
    """从归档读事件 —— 与 SSE 回放同一份数据。"""
    from atlas_server.db.models import RunEvent
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(RunEvent).where(RunEvent.run_id == UUID(run_id)).order_by(RunEvent.seq)
            )
        ).scalars()
        return [{"seq": r.seq, "type": r.type, "data": r.data} for r in rows]


# ──────────────────────────────────────────────── 全链路


async def test_acp_turn_produces_native_shaped_events() -> None:
    """★ 验收标准：事件与 native 同形，前端一行不改。

    逐字段看 data —— 前端的 reducer 按字段名取值，少一个就是要加分支。
    """
    real_thread = await setup_thread()
    async with acp_stack(real_thread) as (client, _pods):
        result = await run_turn(client, real_thread, "帮我改个文件")
        assert result["status"] == "succeeded", result

        events = await archived(result["id"])
        kinds = [e["type"] for e in events]

        # 与 native 同一批事件类型
        assert kinds[0] == EventType.RUN_STARTED
        assert EventType.MESSAGE_DELTA in kinds
        assert EventType.THINKING_DELTA in kinds  # CLI 的思考走这条，不污染正文
        assert EventType.MESSAGE_COMPLETED in kinds
        assert EventType.USAGE_UPDATED in kinds
        assert kinds[-1] == EventType.RUN_FINISHED

        # seq 从 1 严格递增无空洞（契约规则 2）—— 两个 runtime 共用 EventFactory
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))

        delta = next(e for e in events if e["type"] == EventType.MESSAGE_DELTA)
        # native 的 message.delta 就是这两个键 —— block 是段号，前端靠它把
        # 「调工具前说的话」与「最终回答」分开渲染（domain/events.py::Answer）
        assert set(delta["data"]) == {"text", "block"}

        completed = next(e for e in events if e["type"] == EventType.MESSAGE_COMPLETED)
        assert completed["data"]["content"] == [{"type": "text", "text": "好的，做完了。"}]

        usage = next(e for e in events if e["type"] == EventType.USAGE_UPDATED)
        assert usage["data"]["total_tokens"] == 15  # 键名与 normalize_usage 一致

        finished = events[-1]
        assert finished["data"]["stop_reason"] == "end_turn"
        assert finished["data"]["text_len"] == len("好的，做完了。")


async def test_thinking_never_leaks_into_the_answer() -> None:
    """CLI 的思考不能被拼进 assistant 消息。

    混进去的话用户会看到一段与回答不连贯的自言自语 —— 这正是
    agent_thought_chunk 走 thinking.delta 而不是 message.delta 的理由。
    """
    real_thread = await setup_thread()
    async with acp_stack(real_thread) as (client, _):
        result = await run_turn(client, real_thread, "去")
        events = await archived(result["id"])

    completed = next(e for e in events if e["type"] == EventType.MESSAGE_COMPLETED)
    assert "先看代码" not in completed["data"]["content"][0]["text"]
    # 但它确实作为 thinking 流出来了
    thinking = [e for e in events if e["type"] == EventType.THINKING_DELTA]
    assert thinking and thinking[0]["data"]["text"] == "先看代码"


async def test_assistant_message_is_persisted_like_native() -> None:
    """落库路径与 native 共用 —— acp 的回答同样进 message 表。"""
    from atlas_server.db.models import Message
    from sqlalchemy import select

    real_thread = await setup_thread()
    async with acp_stack(real_thread) as (client, _):
        await run_turn(client, real_thread, "去")

    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.thread_id == UUID(real_thread), Message.role == "assistant"
                    )
                )
            ).scalars()
        )
    assert rows and "做完了" in json.dumps(rows[0].content, ensure_ascii=False)


# ──────────────────────────────────────────────── 会话身份


async def test_external_session_id_is_remembered_for_the_next_turn() -> None:
    """★ CLI 会话 id 必须当场落下 —— 丢了就等于每轮冷启动。"""
    from atlas_server.db.models import Thread

    real_thread = await setup_thread()
    async with acp_stack(real_thread) as (client, _):
        await run_turn(client, real_thread, "第一轮")

        async with get_sessionmaker()() as session:
            thread = await session.get(Thread, UUID(real_thread))
            assert thread is not None
            assert thread.external_session_id == "fake-session-1"

        # 第二轮：已有 id 且假 adapter 声明 can_resume → 走 load，不发 lost
        result = await run_turn(client, real_thread, "第二轮")
        kinds = [e["type"] for e in await archived(result["id"])]
        assert EventType.SESSION_LOST not in kinds


async def test_replayed_history_does_not_leak_into_this_turn() -> None:
    """★ session/load 重放的历史**不属于本轮**。

    ACP 规定 load 要先把整段会话以 session/update 重放一遍再回响应。那些帧
    与本轮的 update 走同一个队列 —— 不切边界的话，_pump 会把重放的正文一并
    累加进 text_parts，落库的助手消息就成了「历史 + 本轮」的拼接：用户问
    「文件内容是什么」，看到的却是前两轮的回答接着这轮的回答。

    真 CLI 上稳定复现，而假 adapter 早先不重放，所以这条路一直没人走过。
    """
    from atlas_server.db.models import Message
    from sqlalchemy import select

    from .fake_adapter import REPLAYED_TEXT

    real_thread = await setup_thread()
    async with acp_stack(real_thread) as (client, _):
        await run_turn(client, real_thread, "第一轮")
        result = await run_turn(client, real_thread, "第二轮")  # 这一轮走 load

        # ① 不进事件流：否则 UI 上看着像本轮又把上一轮的活干了一遍
        blob = json.dumps([e["data"] for e in await archived(result["id"])], ensure_ascii=False)
        assert REPLAYED_TEXT not in blob, "重放的历史漏进了本轮的事件流"

        # ② 不进落库的助手消息 —— 这是用户直接看到的那一份
        async with get_sessionmaker()() as session:
            rows = list(
                (
                    await session.execute(
                        select(Message).where(
                            Message.thread_id == UUID(real_thread), Message.role == "assistant"
                        )
                    )
                ).scalars()
            )
        latest = json.dumps(rows[-1].content, ensure_ascii=False)
        assert REPLAYED_TEXT not in latest, f"助手消息把历史拼了进来：{latest}"
        assert "做完了" in latest, "本轮自己的回答反而丢了"


# ──────────────────────────────────────────────── 审批回路


async def test_permission_request_becomes_a_platform_approval() -> None:
    """★ acp 的审批与 native 汇入同一张 Approval 表、同一个前端弹窗。"""
    from atlas_server.db.models import Approval
    from sqlalchemy import select

    real_thread = await setup_thread()
    async with acp_stack(real_thread, ACP_FAKE_ASK_PERMISSION="1") as (client, _):

        accepted = await client.post(
            f"/v1/threads/{real_thread}/runs",
            json={"content": [{"type": "text", "text": "写个文件"}]},
        )
        run_id = accepted.json()["run_id"]

        # 等审批出现，然后批准 —— 与 native 走同一个 API
        approval_id = None
        for _ in range(200):
            await asyncio.sleep(0.05)
            async with get_sessionmaker()() as session:
                rows = list(
                    (
                        await session.execute(
                            select(Approval).where(Approval.run_id == UUID(run_id))
                        )
                    ).scalars()
                )
            if rows:
                approval_id = rows[0].id
                break
        assert approval_id is not None, "acp 的权限请求没有变成平台 Approval"

        resp = await client.post(
            f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": "approved"}
        )
        assert resp.status_code in (200, 204), resp.text

        result = await run_turn_wait(client, str(run_id))
        assert result["status"] == "succeeded", result
        events = await archived(str(run_id))
        blob = json.dumps([e["data"] for e in events], ensure_ascii=False)
        # 用户的决定原样回到了 CLI
        assert "[决定=allow]" in blob

        # ★ 落了库还不够，必须**发事件**。前端的审批弹窗由 approval.required
        #   驱动（web/src/lib/events.ts 的 ApprovalRequired），只查 Approval 表
        #   的断言看不见「事件没发」这种缺口 —— 而它的后果是每个需要授权的
        #   acp run 都没人应答，一路卡到 bridge 的 adapter 超时才以
        #   runtime_crashed 失败。真 CLI 上跑第一轮就撞上了。
        asked = [e for e in events if e["type"] == EventType.APPROVAL_REQUIRED.value]
        assert asked, "审批落了库却没进事件流 —— 前端永远收不到弹窗信号"
        # 字段名与 native 逐字对齐：前端 reducer 按名字取值，少一个就要加分支
        assert asked[0]["data"].keys() >= {"approval_id", "tool_name", "args"}
        assert asked[0]["data"]["approval_id"] == str(approval_id)


async def run_turn_wait(client: httpx.AsyncClient, run_id: str) -> dict:
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = asyncio.get_running_loop().time() + 25.0
    while asyncio.get_running_loop().time() < deadline:
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in terminal:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 超时未结束")


# ──────────────────────────────────────────────── 故障


async def test_adapter_crash_becomes_a_retryable_run_failure() -> None:
    real_thread = await setup_thread()
    async with acp_stack(real_thread, ACP_FAKE_CRASH_ON="session/prompt") as (client, _):
        result = await run_turn(client, real_thread, "去")
        assert result["status"] == "failed"
        assert result["error_kind"] in ("runtime_crashed", "runtime_unreachable")


async def test_unreachable_pod_fails_the_run_not_the_process() -> None:
    """Pod 连不上是一次 run 失败，不是执行器崩溃。"""
    from atlas_server.acp.pods import PodEndpoint

    class DeadPods:
        async def ensure(self, thread: Any, cli: Any) -> PodEndpoint:
            return PodEndpoint(url=f"ws://127.0.0.1:{_free_port()}", token=TOKEN)

    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        get_settings(),
        model_builder=lambda *_a, **_k: None,
        acp_runtime=AcpRuntime(get_sessionmaker(), get_settings(), DeadPods()),
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await make_acp_agent(client)
        thread_id = (
            await client.post("/v1/threads", json={"agent_id": agent_id})
        ).json()["id"]
        result = await run_turn(client, thread_id, "去")
        assert result["status"] == "failed"
        assert result["error_kind"] == "runtime_unreachable"
