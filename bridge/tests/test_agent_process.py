"""AgentProcess：对真实子进程测试（代码设计 §7.3）。

agent 是以独立进程运行的 FakeAgent，走真实的 stdio —— 超长行、stderr、进程组终止、
环境隔离这些问题只有真进程才会暴露。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from atlas_bridge.agent.process import AgentLaunch, AgentProcess, OversizeLine
from atlas_bridge.testing.fake_agent import Script
from atlas_jsonrpc import ChannelClosed


def launch(script: Script, tmp_path: Path, **kw: object) -> AgentLaunch:
    return AgentLaunch(
        argv=(sys.executable, "-m", "atlas_bridge.testing.fake_agent", script.to_json()),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ONLY_THIS": "1"},
        oom_score_adj=None,
        **kw,  # type: ignore[arg-type]
    )


async def request(proc: AgentProcess, rid: int, method: str, params: dict) -> dict:
    await proc.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
    while True:
        line = await asyncio.wait_for(proc.receive(), 10)
        assert line is not None
        frame = json.loads(line)
        if frame.get("id") == rid and "method" not in frame:
            return frame


async def test_line_roundtrip(tmp_path: Path) -> None:
    proc = AgentProcess(launch(Script(), tmp_path))
    await proc.start()
    try:
        resp = await request(proc, 1, "initialize", {"protocolVersion": 1})
        assert resp["result"]["protocolVersion"] == 1
    finally:
        await proc.terminate(grace=2)


async def test_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ agent 只拿到白名单里的环境变量，拿不到 bridge 自己的（Bridge 设计 §8.3）。"""
    monkeypatch.setenv("BRIDGE_SECRET", "must-not-leak")
    proc = AgentProcess(launch(Script(), tmp_path))
    await proc.start()
    try:
        keys = (await request(proc, 1, "_test/env", {}))["result"]["keys"]
        assert "ONLY_THIS" in keys and "BRIDGE_SECRET" not in keys
    finally:
        await proc.terminate(grace=2)


async def test_oversize_line_is_skipped_and_the_next_frame_survives(tmp_path: Path) -> None:
    """★ 一行 200 KB 的 update（上限 64 KB），紧跟着一个权限请求：只丢超长的那一行。

    asyncio 的 readline 超限时会清空整个缓冲区，把后面完整的权限请求一起丢掉。
    """
    oversize: list[OversizeLine] = []
    script = Script(updates_per_turn=0, huge_line_bytes=200_000, ask_permission=True)
    proc = AgentProcess(
        launch(script, tmp_path, max_line_bytes=64 * 1024), on_oversize=oversize.append
    )
    await proc.start()
    try:
        await request(proc, 1, "initialize", {"protocolVersion": 1})
        sid = (await request(proc, 2, "session/new", {"cwd": "/w", "mcpServers": []}))["result"][
            "sessionId"
        ]
        await proc.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
                }
            )
        )
        methods = []
        while "session/request_permission" not in methods:
            line = await asyncio.wait_for(proc.receive(), 10)
            assert line is not None
            methods.append(json.loads(line).get("method"))
        assert len(oversize) == 1
        assert oversize[0].size > 200_000
        assert b'"sessionUpdate"' in oversize[0].head or b"session/update" in oversize[0].head
    finally:
        await proc.terminate(grace=2)


async def test_stderr_is_collected_and_truncated(tmp_path: Path) -> None:
    lines: list[str] = []
    script = Script(stderr_lines=["hello from agent", "y" * 5000])
    proc = AgentProcess(launch(script, tmp_path, stderr_line_bytes=100), on_stderr=lines.append)
    await proc.start()
    await request(proc, 1, "initialize", {})
    await proc.terminate(grace=2)
    assert lines[0] == "hello from agent"
    assert len(lines[1]) == 100


async def test_terminate_kills_the_whole_process_group(tmp_path: Path) -> None:
    """★ CLI 拉起的子进程也必须一起终止，不能留成孤儿。"""
    lines: list[str] = []
    proc = AgentProcess(launch(Script(spawn_child=True), tmp_path), on_stderr=lines.append)
    await proc.start()
    await request(proc, 1, "initialize", {})
    child_pid = int(next(ln for ln in lines if ln.startswith("child-pid=")).split("=")[1])
    os.kill(child_pid, 0)  # 子进程确实在运行
    await proc.terminate(grace=2)
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


async def test_exit_is_observable_and_send_after_exit_fails(tmp_path: Path) -> None:
    proc = AgentProcess(launch(Script(on_prompt="crash"), tmp_path))
    await proc.start()
    await request(proc, 1, "initialize", {})
    sid = (await request(proc, 2, "session/new", {"cwd": "/w", "mcpServers": []}))["result"][
        "sessionId"
    ]
    await proc.send(
        json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": sid}}
        )
    )
    assert await asyncio.wait_for(proc.receive(), 10) is None  # stdout 关闭
    assert await asyncio.wait_for(proc.wait(), 10) == 3
    with pytest.raises(ChannelClosed):
        await proc.send("{}")


async def test_sigterm_ignored_escalates_to_sigkill(tmp_path: Path) -> None:
    """SIGTERM 不管用时，宽限期后 SIGKILL（Bridge 设计 §6.1）。"""
    ignore_term = (
        "import signal,sys,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('ready',flush=True);time.sleep(60)"
    )
    proc = AgentProcess(
        AgentLaunch(
            argv=(sys.executable, "-c", ignore_term),
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            oom_score_adj=None,
        )
    )
    await proc.start()
    assert await asyncio.wait_for(proc.receive(), 10) == "ready"
    loop = asyncio.get_running_loop()
    started = loop.time()
    code = await proc.terminate(grace=0.5)
    assert code < 0 and loop.time() - started < 5
