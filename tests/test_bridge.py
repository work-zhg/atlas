"""Bridge 的三件本地事务（acp 详设 §12 步骤 3）。

验证清单来自设计：
  · 配对拒绝无 token 的握手
  · 单活跃 prompt：第二个请求直接拒
  · 假 adapter 崩溃 → 可重试的失败

★ 不需要 K8s、不需要 Docker：bridge 在本进程起 WS 服务，adapter 是
  tests/fake_adapter.py 这个脚本化子进程。真 CLI 要镜像与 API key，
  而这三件事跟 CLI 是谁无关 —— 它们是 bridge 自己的逻辑。
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

import pytest
from atlas_acp.wire import ErrorCode, Method
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from atlas_bridge.adapter import AdapterProcess
from atlas_bridge.ws import CLOSE_SUPERSEDED, CLOSE_UNAUTHORIZED, BridgeServer

TOKEN = "pod-secret-token"
THREAD_ID = "11111111-1111-1111-1111-111111111111"
_FAKE = str(Path(__file__).parent / "fake_adapter.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class BridgeHarness:
    """起一个 bridge（WS + 假 adapter），测完拆干净。"""

    def __init__(self, port: int, server: BridgeServer, adapter: AdapterProcess) -> None:
        self.port = port
        self.server = server
        self.adapter = adapter

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def headers(self, *, token: str | None = TOKEN, thread_id: str = THREAD_ID) -> dict[str, str]:
        out: dict[str, str] = {"X-Thread-Id": thread_id}
        if token is not None:
            out["Authorization"] = f"Bearer {token}"
        return out


@contextlib.asynccontextmanager
async def bridge(**script: str) -> AsyncIterator[BridgeHarness]:
    """按剧本起一个 bridge。script 的键值进假 adapter 的环境变量。"""
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
    server = BridgeServer(adapter, token=TOKEN, thread_id=THREAD_ID, request_timeout_s=10)
    await adapter.start()
    serving = asyncio.create_task(server.serve_forever("127.0.0.1", port))
    await asyncio.sleep(0.15)  # 等端口就绪
    try:
        yield BridgeHarness(port, server, adapter)
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        await adapter.stop()


#: initialize 的合法 params。★ 真 adapter 按 schema 校验，少了 protocolVersion
#: 就回 -32602 —— 所以测试也必须发合法的那一份，否则等于把"参数对不对"
#: 排除在覆盖之外（生产代码正是在这里错了很久）。
INIT_PARAMS: dict[str, Any] = {"protocolVersion": 1, "clientCapabilities": {}}


async def _call(ws, method: str, params: dict[str, Any] | None = None, *, frame_id: int = 1):
    """发一个请求，读到**它的响应**为止 —— 中间的 update 通知收集起来返回。"""
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "id": frame_id, "method": method, "params": params or {}})
    )
    updates: list[dict[str, Any]] = []
    async with asyncio.timeout(10):
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("id") == frame_id and "method" not in frame:
                return frame, updates
            updates.append(frame)


# ──────────────────────────────────────────────── ① 配对


async def test_handshake_without_token_is_refused() -> None:
    """★ K8s 的默认网络是平的：集群里任何 Pod 都能连任何 Pod 的端口。

    不校验的话，别的租户的会话 Pod 就能连上来驱动这个 CLI —— 读它的
    工作区、用它的凭证执行命令。「在 Pod 网络里」不是安全边界。
    """
    async with bridge() as h:
        with pytest.raises((ConnectionClosed, InvalidStatus, OSError)):
            async with connect(h.url(), additional_headers={"X-Thread-Id": THREAD_ID}) as ws:
                await ws.recv()


async def test_handshake_with_wrong_token_is_refused() -> None:
    async with bridge() as h:
        with pytest.raises((ConnectionClosed, InvalidStatus, OSError)):
            async with connect(h.url(), additional_headers=h.headers(token="guessed")) as ws:
                await ws.recv()


async def test_handshake_for_another_session_is_refused() -> None:
    """会话绑定：本 Pod 只接受针对**它所属会话**的指令。

    token 对但 thread 不对，说明有人拿着一个 Pod 的凭证去驱动另一个会话 ——
    这正是凭证泄漏后最该挡住的那一步。
    """
    async with bridge() as h:
        with pytest.raises((ConnectionClosed, InvalidStatus, OSError)):
            async with connect(h.url(), additional_headers=h.headers(thread_id="other")) as ws:
                await ws.recv()


async def test_valid_handshake_can_initialize() -> None:
    async with bridge() as h, connect(h.url(), additional_headers=h.headers()) as ws:
        frame, _ = await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        caps = frame["result"]["agentCapabilities"]
        assert caps["loadSession"] is True
        # 反向文件通道默认全关 —— 打开等于允许 CLI 绕过审批改宿主文件
        assert caps["fs"]["writeTextFile"] is False


async def test_close_code_is_unauthorized_not_a_generic_error() -> None:
    """拒在**握手**上而不是连上再报错：扫描者连一个可用通道都拿不到。"""
    async with bridge() as h:
        try:
            async with connect(h.url(), additional_headers={"X-Thread-Id": THREAD_ID}) as ws:
                await ws.recv()
        except ConnectionClosed as exc:
            assert exc.rcvd is not None and exc.rcvd.code == CLOSE_UNAUTHORIZED
        except (InvalidStatus, OSError):
            pass  # 握手层直接拒，同样合格


# ──────────────────────────────────────────────── ② 单活跃 prompt


async def test_second_prompt_is_rejected_not_queued() -> None:
    """★ 第二个 prompt 直接拒，不排队。

    排队会让会话串行锁的超时语义变得不可解释（锁按一轮算，排队后一轮
    可能等上任意长）。拒绝是立刻可见的，模型能改派或合并。
    """
    async with bridge(ACP_FAKE_PROMPT_DELAY_S="1.0") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        new, _ = await _call(ws, Method.SESSION_NEW, {"cwd": "/workspace"}, frame_id=2)
        session_id = new["result"]["sessionId"]

        # 第一个 prompt 开跑（假 adapter 会停 1s）
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": Method.SESSION_PROMPT,
                    "params": {"sessionId": session_id, "prompt": []},
                }
            )
        )
        await asyncio.sleep(0.25)  # 确保第一个已进入 busy

        second, _ = await _call(
            ws, Method.SESSION_PROMPT, {"sessionId": session_id, "prompt": []}, frame_id=4
        )
        assert second["error"]["code"] == ErrorCode.SESSION_BUSY
        assert "串行" in second["error"]["message"]


async def test_the_first_prompt_still_completes_after_the_rejection() -> None:
    """★ 拒绝第二个**不能**毁掉第一个。

    这正是「回 JSON-RPC 错误而不是关连接」的理由：关掉会把第一轮正在
    流式输出的 session/update 一起掐断。
    """
    async with bridge(ACP_FAKE_PROMPT_DELAY_S="0.6") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        new, _ = await _call(ws, Method.SESSION_NEW, frame_id=2)
        session_id = new["result"]["sessionId"]

        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": Method.SESSION_PROMPT,
                    "params": {"sessionId": session_id, "prompt": []},
                }
            )
        )
        await asyncio.sleep(0.2)
        # ★ 等第二个 prompt 的响应时会先读到第一轮已经发出的 update ——
        #   它们由 _call 一并带回来，必须算进正文，否则这条断言测的是
        #   「剩下半截」而不是「流完整」。
        rejected, early = await _call(
            ws, Method.SESSION_PROMPT, {"sessionId": session_id}, frame_id=4
        )
        assert rejected["error"]["code"] == ErrorCode.SESSION_BUSY

        # 第一轮照常走完，update 流完整
        texts: list[str] = [
            f["params"]["update"]["content"]["text"]
            for f in early
            if f.get("method") == Method.SESSION_UPDATE
            and f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        ]
        async with asyncio.timeout(10):
            while True:
                frame = json.loads(await ws.recv())
                if frame.get("id") == 3:
                    assert frame["result"]["stopReason"] == "end_turn"
                    break
                if frame.get("method") == Method.SESSION_UPDATE:
                    update = frame["params"]["update"]
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        texts.append(update["content"]["text"])
        assert "".join(texts) == "好的，做完了。"


async def test_prompt_gate_reopens_after_a_turn() -> None:
    """一轮结束后门要重新打开 —— 否则会话只能跑一轮。"""
    async with bridge() as h, connect(h.url(), additional_headers=h.headers()) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        new, _ = await _call(ws, Method.SESSION_NEW, frame_id=2)
        sid = new["result"]["sessionId"]
        first, _ = await _call(ws, Method.SESSION_PROMPT, {"sessionId": sid}, frame_id=3)
        second, _ = await _call(ws, Method.SESSION_PROMPT, {"sessionId": sid}, frame_id=4)
        assert first["result"]["stopReason"] == "end_turn"
        assert second["result"]["stopReason"] == "end_turn"


# ──────────────────────────────────────────────── ③ 崩溃


async def test_adapter_crash_surfaces_as_an_error_not_a_hang() -> None:
    """★ adapter 崩了要让调用方**立刻**知道，不是挂到超时。

    挂住的表现是「CLI 没反应」，而实际上进程早没了 —— 这种失败最难查。
    """
    async with bridge(ACP_FAKE_CRASH_ON="session/new") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        frame, _ = await _call(ws, Method.SESSION_NEW, frame_id=2)
        assert "error" in frame
        assert frame["error"]["code"] == ErrorCode.INTERNAL_ERROR


async def test_crash_is_reported_with_exit_code_for_diagnosis() -> None:
    """崩溃原因要带退出码与 stderr 尾巴 —— 否则排查只能靠猜。"""
    async with bridge(ACP_FAKE_CRASH_ON="initialize") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        crash = await asyncio.wait_for(h.adapter.wait_closed(), timeout=5)
        assert crash is not None
        assert "9" in str(crash)  # 假 adapter 用 exit code 9


async def test_pending_requests_wake_up_on_crash() -> None:
    """崩溃时还在等响应的调用方必须被唤醒，不能各自挂到超时。"""
    async with bridge(ACP_FAKE_CRASH_ON="session/new") as h:
        await _call_direct(h)


async def _call_direct(h: BridgeHarness) -> None:
    from atlas_bridge.adapter import AdapterCrashed

    await h.adapter.request(Method.INITIALIZE, INIT_PARAMS, timeout=5)
    with pytest.raises(AdapterCrashed):
        await h.adapter.request(Method.SESSION_NEW, {}, timeout=30)


# ──────────────────────────────────────────────── 审批转交


async def test_permission_request_is_relayed_never_auto_allowed() -> None:
    """★ 红线：bridge 默认**转交**，绝不自动 allow。

    一旦为了省事在这里写死自动批准，前端那套审批卡片就成了摆设 ——
    任意文件写入与命令执行的权限实际上交给了模型。
    """
    async with bridge(ACP_FAKE_ASK_PERMISSION="1") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        new, _ = await _call(ws, Method.SESSION_NEW, frame_id=2)
        sid = new["result"]["sessionId"]

        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": Method.SESSION_PROMPT,
                    "params": {"sessionId": sid, "prompt": []},
                }
            )
        )

        saw_request = False
        texts: list[str] = []
        async with asyncio.timeout(10):
            while True:
                frame = json.loads(await ws.recv())
                # bridge 把 adapter 的反向请求推给 server（这里是测试扮演 server）
                if frame.get("method") == Method.SESSION_REQUEST_PERMISSION:
                    saw_request = True
                    assert frame["params"]["toolCall"]["toolCallId"] == "tc-1"
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": frame["id"],
                                "result": {"outcome": {"outcome": "selected",
                                                       "optionId": "reject"}},
                            }
                        )
                    )
                    continue
                if frame.get("method") == Method.SESSION_UPDATE:
                    update = frame["params"]["update"]
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        texts.append(update["content"]["text"])
                    continue
                if frame.get("id") == 3:
                    break

        assert saw_request, "bridge 没有把 request_permission 转交出来"
        # 用户的决定原样回到了 CLI
        assert "[决定=reject]" in "".join(texts)


async def test_permission_without_a_server_connection_is_not_an_allow() -> None:
    """没人能批准时当作拒绝，不是放行。"""
    from atlas_bridge.adapter import AdapterCrashed

    async with bridge() as h:
        with pytest.raises(AdapterCrashed):
            await h.server.on_adapter_request(
                {"method": Method.SESSION_REQUEST_PERMISSION, "params": {}}
            )


# ──────────────────────────────────────────────── 健壮性


async def test_non_json_banner_on_stdout_does_not_break_the_bridge() -> None:
    """真 CLI 常往 stdout 漏启动横幅 —— 一行非 JSON 不该噎死整条链路。"""
    async with bridge(ACP_FAKE_BANNER="1") as h, connect(
        h.url(), additional_headers=h.headers()
    ) as ws:
        frame, _ = await _call(ws, Method.INITIALIZE, INIT_PARAMS)
        assert frame["result"]["protocolVersion"] == 1


async def test_reconnect_supersedes_the_previous_connection() -> None:
    """★ 新连接取代旧的，而不是被拒。

    server 重启后会重连，而旧连接的 TCP 可能几分钟才判死。拒新的话 Pod
    在这段时间里完全不可达 —— 而这恰恰是最需要它可达的时刻。
    """
    async with bridge() as h:
        first = await connect(h.url(), additional_headers=h.headers())
        second = await connect(h.url(), additional_headers=h.headers())
        try:
            with pytest.raises(ConnectionClosed) as caught:
                async with asyncio.timeout(5):
                    await first.recv()
            assert caught.value.rcvd is not None
            assert caught.value.rcvd.code == CLOSE_SUPERSEDED
            # 新连接可用
            frame, _ = await _call(second, Method.INITIALIZE, INIT_PARAMS)
            assert frame["result"]["protocolVersion"] == 1
        finally:
            await second.close()
            with contextlib.suppress(Exception):
                await first.close()
