"""sandboxd：执行、幂等接续、取消、卡死判定、输出截断、鉴权、屏障。

真起子进程（bash），不 mock 执行 —— 进程组、管道、信号这些正是要测的东西。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from atlas_sandboxd.barrier import BarrierClient, serve_barrier
from atlas_sandboxd.config import BarrierConfig, ServeConfig
from atlas_sandboxd.execs import ExecManager, ExecRequest
from atlas_sandboxd.server import make_server

TOKEN = "t0ken"
SESSION = "thread-1"


def _wait(ex: Any, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while ex.state != "exited":
        assert time.monotonic() < deadline, "命令没在预期时间内结束"
        time.sleep(0.05)
    return ex.result


@pytest.fixture
def cfg(tmp_path: Path) -> ServeConfig:
    ws = tmp_path / "ws"
    ws.mkdir()
    token = tmp_path / "token"
    token.write_text(TOKEN)
    return ServeConfig(
        port=0,
        host="127.0.0.1",
        token_file=str(token),
        session_id=SESSION,
        workspace=str(ws),
        log_dir=str(tmp_path / "logs"),
        output_limit_bytes=1000,
        idle_s=0,
        kill_grace_s=0.5,
        heartbeat_s=0.2,
    )


@pytest.fixture
def manager(cfg: ServeConfig) -> Iterator[ExecManager]:
    m = ExecManager(cfg)
    yield m
    m.close()


def test_exec_runs_in_workspace_with_exit_code(manager: ExecManager, cfg: ServeConfig) -> None:
    ex, created = manager.start(ExecRequest("r:1", "pwd; echo hi >&2; exit 3"))
    res = _wait(ex)
    assert created
    assert res["exit_code"] == 3
    assert cfg.workspace in res["output"] and "hi" in res["output"]
    assert not res["truncated"] and res["flushed"] is None


def test_same_exec_id_is_not_rerun(manager: ExecManager, cfg: ServeConfig) -> None:
    marker = Path(cfg.workspace) / "count"
    cmd = f"echo x >> {marker}; sleep 0.3"
    ex1, c1 = manager.start(ExecRequest("r:dup", cmd))
    ex2, c2 = manager.start(ExecRequest("r:dup", cmd))
    _wait(ex1)
    assert c1 and not c2 and ex1 is ex2
    assert marker.read_text().count("x") == 1


def test_output_is_truncated_head_and_tail(manager: ExecManager) -> None:
    ex, _ = manager.start(ExecRequest("r:big", "echo START; seq 1 5000; echo END"))
    res = _wait(ex)
    assert res["truncated"] and res["output_bytes"] > 1000
    assert res["output"].startswith("START") and res["output"].rstrip().endswith("END")
    assert "省略" in res["output"]
    assert Path(res["log_path"]).read_text().count("\n") >= 5002


def test_kill_run_terminates_process_group(manager: ExecManager, cfg: ServeConfig) -> None:
    child = Path(cfg.workspace) / "child-alive"
    # shell 里起一个子进程：只杀 shell 的话它会活下来继续写文件
    ex, _ = manager.start(
        ExecRequest("run9:a", f"(sleep 1; touch {child}) & sleep 30", run_id="run9")
    )
    time.sleep(0.3)
    assert manager.kill_run("run9") == ["run9:a"]
    res = _wait(ex)
    assert res["killed"] and res["signal"] is not None
    time.sleep(1.2)
    assert not child.exists()


def test_stall_detection_without_output_or_cpu(cfg: ServeConfig) -> None:
    m = ExecManager(cfg, cpu_of=lambda _pgid: 0.0)  # CPU 恒定不涨
    try:
        ex, _ = m.start(ExecRequest("r:stall", "sleep 30", idle_s=1.5))
        res = _wait(ex, timeout=10)
        assert res["stalled"] and "卡死" in " ".join(res["notes"])
    finally:
        m.close()


def test_busy_command_is_not_stalled(cfg: ServeConfig) -> None:
    ticks = iter(range(10_000))
    m = ExecManager(cfg, cpu_of=lambda _pgid: float(next(ticks)))  # CPU 一直在涨
    try:
        ex, _ = m.start(ExecRequest("r:busy", "sleep 3", idle_s=1))
        res = _wait(ex, timeout=10)
        assert res["exit_code"] == 0 and not res["stalled"]
    finally:
        m.close()


def test_explicit_timeout(manager: ExecManager) -> None:
    ex, _ = manager.start(ExecRequest("r:to", "sleep 30", timeout_s=1))
    res = _wait(ex)
    assert res["timed_out"]


def test_background_process_does_not_block_completion(
    manager: ExecManager, cfg: ServeConfig
) -> None:
    # 后台进程继承了 stdout：以管道 EOF 为结束的话这条命令永远不会返回
    ex, _ = manager.start(ExecRequest("r:bg", "sleep 20 & echo started"))
    res = _wait(ex, timeout=5)
    assert res["exit_code"] == 0 and "started" in res["output"]


def test_env_does_not_leak_daemon_config(
    manager: ExecManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDBOXD_TOKEN_FILE", "/secret")
    monkeypatch.setenv("IMAGE_DEFINED", "kept")  # 镜像自己的变量（JAVA_HOME 之类）要照传
    ex, _ = manager.start(
        ExecRequest(
            "r:env",
            "env | grep -E '^(FOO|IMAGE_DEFINED|SANDBOXD_)' || true",
            env={"FOO": "bar", "SANDBOXD_X": "no"},
        )
    )
    out = _wait(ex)["output"]
    assert "FOO=bar" in out and "IMAGE_DEFINED=kept" in out
    assert "SANDBOXD_" not in out


# ─────────────────────────────────────────────── HTTP


@pytest.fixture
def server(cfg: ServeConfig, manager: ExecManager) -> Iterator[int]:
    srv = make_server(cfg, manager)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _request(
    port: int, method: str, path: str, body: dict[str, Any] | None = None, **headers: str
) -> tuple[int, list[dict[str, Any]]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Authorization": f"Bearer {TOKEN}", "X-Atlas-Sandbox": SESSION, **headers}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return resp.status, lines


def test_http_stream_and_reattach(server: int) -> None:
    status, events = _request(
        server, "POST", "/v1/execs", {"exec_id": "r:h1", "command": "echo one; echo two"}
    )
    assert status == 200
    kinds = [e["t"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "exited"
    assert events[-1]["exit_code"] == 0 and "two" in events[-1]["output"]
    # 断线后接上：同一个 exec_id 再 POST 不重跑，从 after 之后回放
    last_output = max(e["seq"] for e in events if e["t"] == "output")
    status, again = _request(server, "GET", f"/v1/execs/r:h1?after={last_output}")
    assert status == 200 and [e["t"] for e in again] == ["exited"]


def test_http_heartbeat_while_silent(server: int) -> None:
    _, events = _request(server, "POST", "/v1/execs", {"exec_id": "r:hb", "command": "sleep 1"})
    assert any(e["t"] == "heartbeat" for e in events)


def test_http_auth(server: int) -> None:
    status, _ = _request(server, "GET", "/v1/info", Authorization="Bearer wrong")
    assert status == 401
    status, _ = _request(server, "GET", "/v1/info", **{"X-Atlas-Sandbox": "other"})
    assert status == 403
    status, info = _request(server, "GET", "/v1/info")
    assert status == 200 and info[0]["boot_id"]
    status, _ = _request(server, "GET", "/healthz", Authorization="")
    assert status == 200


# ─────────────────────────────────────────────── 屏障


class _FakeRc:
    """假的 rclone 远程控制：记录调用；vfs/queue 前两次返回有待上传项。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.queue_polls = 0

    def serve(self) -> int:
        rc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                rc.calls.append((self.path, body, self.headers.get("Authorization", "")))
                out: dict[str, Any] = {}
                if self.path == "/vfs/queue":
                    rc.queue_polls += 1
                    out = {"queue": [{"name": "a"}] if rc.queue_polls <= 2 else []}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv.server_address[1]


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_barrier_forgets_before_and_waits_flush_after(cfg: ServeConfig) -> None:
    rc = _FakeRc()
    rc_port = rc.serve()
    b_port = _free_port()
    bcfg = BarrierConfig(port=b_port, rc_url=f"http://127.0.0.1:{rc_port}", rc_pass="pw")
    threading.Thread(target=serve_barrier, args=(bcfg,), daemon=True).start()
    time.sleep(0.3)

    m = ExecManager(cfg, barrier=BarrierClient(f"http://127.0.0.1:{b_port}", poll_s=0.05))
    try:
        ex, _ = m.start(ExecRequest("r:b", "echo hi > out.txt"))
        res = _wait(ex)
    finally:
        m.close()
    paths = [c[0] for c in rc.calls]
    assert paths[0] == "/vfs/forget"  # 执行前
    assert paths.count("/vfs/queue") == 3  # 执行后等到队列清空
    assert res["flushed"] is True
    assert any(e["t"] == "flushing" for e in ex.events)
    # rc 口令只在 barrier 里（Basic sbx:pw）
    assert all(c[2].startswith("Basic ") for c in rc.calls)


def test_barrier_unreachable_is_reported_not_hidden(cfg: ServeConfig) -> None:
    m = ExecManager(cfg, barrier=BarrierClient(f"http://127.0.0.1:{_free_port()}", poll_s=0.01))
    try:
        ex, _ = m.start(ExecRequest("r:nb", "true"))
        res = _wait(ex)
    finally:
        m.close()
    assert res["exit_code"] == 0
    assert res["flushed"] is False
    assert any("缓存刷新失败" in n for n in res["notes"])
