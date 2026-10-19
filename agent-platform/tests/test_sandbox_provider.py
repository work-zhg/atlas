"""server 侧的 Shell 沙箱：PodSandbox 对着进程内真起的 sandboxd。

doc/native-sandbox-design.html §8。

不连 K8s：cluster 用假的 ensure_sandbox，直接指向本地 sandboxd —— 被测的是协议、幂等、
断线重连、取消、重建提示与错误语义；Pod 本身由 test_cluster_sandbox 与真集群冒烟覆盖。
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_agent.providers.cluster import ClusterUnavailable
from atlas_agent.providers.pods import PodEndpoint
from atlas_agent.providers.sandbox import PodSandbox
from atlas_engine.contracts import SandboxUnavailable
from atlas_sandboxd.config import ServeConfig
from atlas_sandboxd.execs import ExecManager
from atlas_sandboxd.server import make_server

WS = "ws-thread"


class _Daemon:
    def __init__(self, tmp: Path, name: str) -> None:
        ws = tmp / "ws"
        ws.mkdir(exist_ok=True)
        token = tmp / f"{name}.token"
        token.write_text("tok")
        cfg = ServeConfig(
            port=0,
            host="127.0.0.1",
            token_file=str(token),
            session_id=WS,
            workspace=str(ws),
            log_dir=str(tmp / f"{name}-logs"),
            idle_s=0,
            kill_grace_s=0.3,
            heartbeat_s=0.5,
        )
        self.workspace = ws
        self.manager = ExecManager(cfg)
        self.server = make_server(cfg, self.manager)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = PodEndpoint(
            url=f"http://127.0.0.1:{self.server.server_address[1]}", token="tok"
        )

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()


class _Pods:
    """假的 cluster：按顺序返回端点；记录调用次数。"""

    def __init__(self, *endpoints: PodEndpoint | Exception) -> None:
        self._eps = list(endpoints)
        self.calls: list[dict[str, Any]] = []

    async def ensure_sandbox(self, **kw: Any) -> PodEndpoint:
        self.calls.append(kw)
        ep = self._eps[0] if len(self._eps) == 1 else self._eps.pop(0)
        if isinstance(ep, Exception):
            raise ep
        return ep


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[_Daemon]:
    d = _Daemon(tmp_path, "a")
    yield d
    d.stop()


def _sandbox(pods: _Pods, run: str = "run-1") -> PodSandbox:
    return PodSandbox(pods=pods, workspace_thread_id=WS, user_id="u-1", run_id=run)  # type: ignore[arg-type]


def _dead_endpoint() -> PodEndpoint:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return PodEndpoint(url=f"http://127.0.0.1:{port}", token="tok")


async def test_execute_creates_pod_once_and_returns_output(daemon: _Daemon) -> None:
    pods = _Pods(daemon.endpoint)
    sbx = _sandbox(pods)
    r1 = await sbx.aexecute("echo hello; exit 2", exec_id="c1")
    r2 = await sbx.aexecute("pwd", exec_id="c2")
    assert r1.exit_code == 2 and "hello" in r1.output
    assert str(daemon.workspace) in r2.output
    assert len(pods.calls) == 1  # 按需创建，一个 run 内只 ensure 一次
    assert pods.calls[0]["workspace_thread_id"] == WS


async def test_same_tool_call_is_not_executed_twice(daemon: _Daemon) -> None:
    marker = daemon.workspace / "n"
    sbx = _sandbox(_Pods(daemon.endpoint))
    cmd = f"echo x >> {marker}"
    await sbx.aexecute(cmd, exec_id="same")
    again = _sandbox(_Pods(daemon.endpoint))  # 例如 server 重启后重放同一个 tool_call
    await again.aexecute(cmd, exec_id="same")
    assert marker.read_text().count("x") == 1


async def test_reconnects_when_pod_address_was_stale(daemon: _Daemon) -> None:
    # 第一次拿到的地址已失效（Pod 重建换了 IP）→ 重新 ensure 后接上
    pods = _Pods(_dead_endpoint(), daemon.endpoint)
    r = await _sandbox(pods).aexecute("echo ok", exec_id="c")
    assert "ok" in r.output and len(pods.calls) == 2


async def test_cluster_failure_becomes_tool_error_not_run_failure() -> None:
    pods = _Pods(ClusterUnavailable("该用户已有 10 个会话 Pod（上限 10）"))
    with pytest.raises(SandboxUnavailable, match="上限"):
        await _sandbox(pods).aexecute("true", exec_id="c")


async def test_rebuilt_sandbox_is_announced(tmp_path: Path, daemon: _Daemon) -> None:
    await _sandbox(_Pods(daemon.endpoint)).aexecute("true", exec_id="c1")
    rebuilt = _Daemon(tmp_path, "b")  # 新的 boot_id
    try:
        r = await _sandbox(_Pods(rebuilt.endpoint), run="run-2").aexecute("echo hi", exec_id="c2")
    finally:
        rebuilt.stop()
    assert r.output.startswith("[沙箱环境已重建")
    assert "hi" in r.output


async def test_cancel_kills_the_command(daemon: _Daemon) -> None:
    child = daemon.workspace / "late"
    sbx = _sandbox(_Pods(daemon.endpoint), run="run-c")
    task = asyncio.create_task(sbx.aexecute(f"sleep 2; touch {child}", exec_id="long"))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)
    assert not child.exists()
    ex = daemon.manager.get("run-c:long")
    assert ex is not None and ex.result is not None and ex.result["killed"]


class _BreakOnceAfterStart(httpx.AsyncBaseTransport):
    """第一条流在收到 started 之后断开（模拟网络中断 / Pod 消失）；之后的请求正常转发。"""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._broken = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        if self._broken:
            return resp
        self._broken = True
        inner = resp.stream

        class _Stream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[override]
                async for chunk in inner:  # type: ignore[union-attr]
                    yield chunk
                    if b'"started"' in chunk:
                        raise httpx.ReadError("连接被重置")

            async def aclose(self) -> None:
                await inner.aclose()  # type: ignore[union-attr]

        return httpx.Response(resp.status_code, headers=resp.headers, stream=_Stream())


async def test_restart_mid_command_is_not_rerun(tmp_path: Path, daemon: _Daemon) -> None:
    """流断后重连，发现 Pod 已重建（新 boot_id、不认识这条命令）→ 报错，而不是再跑一遍。"""
    rebuilt = _Daemon(tmp_path, "c")
    try:
        pods = _Pods(daemon.endpoint, rebuilt.endpoint)
        sbx = PodSandbox(
            pods=pods,  # type: ignore[arg-type]
            workspace_thread_id=WS,
            user_id="u-1",
            run_id="run-r",
            transport=_BreakOnceAfterStart(),
        )
        with pytest.raises(SandboxUnavailable, match="重启"):
            await sbx.aexecute("sleep 5", exec_id="x")
        assert rebuilt.manager.get("run-r:x") is None  # 新 Pod 上没有重跑
    finally:
        rebuilt.stop()


async def test_reconnect_resumes_the_same_command(daemon: _Daemon) -> None:
    """流断后重连到同一个 Pod：接上原命令，不重跑。"""
    marker = daemon.workspace / "once"
    sbx = PodSandbox(
        pods=_Pods(daemon.endpoint),  # type: ignore[arg-type]
        workspace_thread_id=WS,
        user_id="u-1",
        run_id="run-s",
        transport=_BreakOnceAfterStart(),
    )
    r = await sbx.aexecute(f"echo x >> {marker}; sleep 1; echo done", exec_id="y")
    assert "done" in r.output and r.exit_code == 0
    assert marker.read_text().count("x") == 1


async def test_assembly_refuses_bash_when_sandbox_disabled() -> None:
    from atlas_agent.errors import CapabilityUnavailable
    from atlas_agent.executor.assembly import SandboxNotEnabled

    assert issubclass(SandboxNotEnabled, CapabilityUnavailable)


def test_execute_tool_gets_runtime_injected() -> None:
    """execute 的 runtime 参数必须被 LangChain 识别为注入参数。

    sandbox 中间件曾带着 `from __future__ import annotations`：注解变成字符串，ToolRuntime 识别不到，
    工具一被调用就报 missing positional argument —— 沙箱接入前它从没真正装配过，评测冒烟才撞出来。
    """
    from atlas_engine.kernel.middleware.sandbox import SandboxMiddleware

    tool = SandboxMiddleware(sandbox=_sandbox(_Pods(_dead_endpoint()))).tools[0]
    assert "runtime" in tool._injected_args_keys
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"command", "timeout"}
