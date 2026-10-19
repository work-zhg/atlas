"""sandboxd：沙箱 Pod 主容器里唯一的进程，server 经它执行命令（doc/native-sandbox-design.html §6）。

为什么不用 K8s exec API（D2）：server 没有、也不该有 K8s 权限；exec 把命令绑在那条连接上 ——
server 重启、网络抖一下，命令就跟着没了，也做不到按 id 幂等、断线接上。

    POST /v1/execs              开始或接上一条命令（按 exec_id 幂等），流式返回 NDJSON 事件：
                                started → output / heartbeat / flushing … → exited
                                重连时带 after（见过的最后一个 seq）和 expect_boot_id
    POST /v1/execs/{id}/kill    结束这条命令的进程组
    GET  /v1/info               boot_id、在跑的命令数、闲置多久（cluster 的闲置回收看它）

执行语义（§6.3）：
    bash -lc           每条命令 setsid 自成一个进程组；结束时杀整组 —— 只杀 shell 会留下孤儿
    stdin = /dev/null  等输入的命令（apt 的确认、git 的口令提示）立即失败，而不是永远挂着
    环境变量            主容器的环境去掉 SANDBOXD_*（守护进程自己的配置）；主容器里本来就没有凭据
    输出                完整的写进日志文件；交给模型的只留头尾
    命令结束            以 shell 退出为准，不以输出管道 EOF 为准（后台进程会一直占着管道）
    时长                不设上限。只判「静默卡死」：既没有输出、进程组的 CPU 时间也不涨
    并发                最多 8 条同时跑，多了排队（并行的子智能体共用一个沙箱）

★ 流断了命令照跑：客户端断开只是不再收事件，执行和结果都在守护进程里，接上就能取回。
"""

from __future__ import annotations

import codecs
import contextlib
import hmac
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from storage import Barrier
from tracing import say

HEARTBEAT_S = 0.2  # 没有事件时多久发一次心跳。Atlas 是 15 秒
IDLE_S = 0.6  # 静默卡死判定。Atlas 是 600 秒
WATCH_S = 0.05  # 巡检间隔。Atlas 是 1 秒
KILL_GRACE_S = 0.3  # SIGTERM 之后多久补 SIGKILL。Atlas 是 5 秒
DETACH_S = 0.2  # shell 退出后再等读线程多久。Atlas 是 0.5 秒
OUTPUT_LIMIT = 4000  # 交给模型的输出上限（头尾各一半）。Atlas 是 100 KB
MAX_CONCURRENCY = 8
FLUSH_POLL_S = 0.05
ENV_DENY = ("SANDBOXD_",)
SESSION_HEADER = "X-Atlas-Sandbox"


def short(command: str, n: int = 48) -> str:
    flat = " ".join(command.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


# ─────────────────────────────────────────────────────────────── 一条命令


@dataclass
class ExecRequest:
    exec_id: str
    command: str
    run_id: str = ""
    timeout_s: float | None = None  # 模型显式给的超时；None = 不限
    idle_s: float | None = None  # 卡死判定；None = 用默认值

    @classmethod
    def parse(cls, body: dict[str, Any]) -> ExecRequest:
        exec_id, command = str(body.get("exec_id") or ""), body.get("command")
        if not exec_id or not isinstance(command, str) or not command.strip():
            raise ValueError("exec_id、command 必填")
        timeout, idle = body.get("timeout_s"), body.get("idle_s")
        return cls(
            exec_id=exec_id,
            command=command,
            run_id=str(body.get("run_id") or ""),
            timeout_s=float(timeout) if timeout else None,
            idle_s=float(idle) if idle is not None else None,
        )


class Exec:
    def __init__(self, req: ExecRequest, log_path: Path) -> None:
        self.req = req
        self.id = req.exec_id
        self.log_path = log_path
        self.state = "queued"  # queued → running → exited
        self.pgid: int | None = None
        self.started_at: float | None = None
        # 事件按 seq 存下来：断线后从 after 接上、server 重启后从头回放，都靠它
        # （真实的有总量上限，超了先裁 output 事件）
        self.events: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        # 输出：头尾各留一半给模型，完整的在日志文件里
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self.last_activity = time.monotonic()
        self.last_cpu: float | None = None
        self.kill_reason: str | None = None  # stalled | timeout | killed
        self.detached = False  # shell 已退出：读线程此后只写日志（后台进程的输出）

    def emit(self, event: dict[str, Any], *, final: bool = False) -> None:
        # ★ exited 和「状态 = exited」在同一把锁里写下。分两次写的话，流可能恰好在中间看一眼：
        #   状态已结束、却没有 exited 事件 —— 对端只能当成断线，白白重连一次
        with self.cond:
            self.events.append({"seq": len(self.events) + 1, **event})
            if final:
                self.state = "exited"
            self.cond.notify_all()

    def stream(self, after: int, cpu: Callable[[], float | None]) -> Iterator[dict[str, Any]]:
        """从 seq > after 开始给事件，直到 exited。等待时每 HEARTBEAT_S 发一次心跳（不入库）。"""
        while True:
            with self.cond:
                if len(self.events) <= after and self.state != "exited":
                    self.cond.wait(timeout=HEARTBEAT_S)
                pending, done = self.events[after:], self.state == "exited"
            for event in pending:
                yield event
                after = event["seq"]
                if event["t"] == "exited":
                    return
            if done:
                return
            if not pending:
                yield {"t": "heartbeat", "cpu_s": cpu(), "output_bytes": self.total}


# ─────────────────────────────────────────────────────────────── 执行


class ExecManager:
    def __init__(
        self, *, env: dict[str, str], workspace: Path, log_dir: Path, barrier: Barrier | None
    ) -> None:
        self.boot_id = uuid.uuid4().hex[:6]  # 守护进程启动时生成：变了 = Pod 重建过
        self._env = env
        self._workspace = workspace
        self._log_dir = log_dir
        self._barrier = barrier
        self._execs: dict[str, Exec] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(MAX_CONCURRENCY)
        self._shell = ["bash", "-lc"] if shutil.which("bash") else ["sh", "-c"]
        self.last_activity = time.monotonic()
        self.dead = False  # 容器没了（Pod 被删）
        self._stop = threading.Event()
        threading.Thread(target=self._watch, name="sandboxd-watch", daemon=True).start()

    def start(self, req: ExecRequest) -> tuple[Exec, bool]:
        """返回 (exec, 是否新建)。★ 同一个 exec_id 已存在 → 接上原来那条，不重跑。"""
        with self._lock:
            if (existing := self._execs.get(req.exec_id)) is not None:
                return existing, False
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in req.exec_id)
            ex = Exec(req, self._log_dir / f"{safe}.log")
            self._execs[req.exec_id] = ex
        threading.Thread(target=self._run, args=(ex,), name=f"exec-{safe}", daemon=True).start()
        return ex, True

    def get(self, exec_id: str) -> Exec | None:
        with self._lock:
            return self._execs.get(exec_id)

    def list(self) -> list[Exec]:
        with self._lock:
            return list(self._execs.values())

    def kill(self, exec_id: str) -> bool:
        ex = self.get(exec_id)
        if ex is None or ex.state == "exited":
            return False
        self._terminate(ex, "killed")
        return True

    def info(self) -> dict[str, Any]:
        with self._lock:
            running = sum(1 for e in self._execs.values() if e.state != "exited")
        # 有命令在跑时闲置时长恒为 0：cluster 永远不回收正在干活的沙箱
        idle = 0.0 if running else round(time.monotonic() - self.last_activity, 2)
        return {"boot_id": self.boot_id, "running": running, "idle_s": idle}

    def cpu(self, ex: Exec) -> float | None:
        return group_cpu_seconds(ex.pgid) if ex.pgid else None

    def shutdown(self) -> None:
        """容器没了：里面的进程一个不剩（K8s 删 Pod 时就是这样）。"""
        self.dead = True
        self._stop.set()
        with self._lock:
            groups = [e.pgid for e in self._execs.values() if e.pgid]
        for pgid in groups:  # 包括已结束命令留下的后台进程
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)

    def _run(self, ex: Exec) -> None:
        with self._slots:  # 并发上限
            self.last_activity = time.monotonic()
            if self._barrier is not None:
                self._barrier.forget()  # ★ 屏障前半：让挂载器丢掉缓存，文件工具刚写的命令才看得到
            env = {k: v for k, v in self._env.items() if not k.startswith(ENV_DENY)}
            log = open(ex.log_path, "wb")  # noqa: SIM115 —— 由读线程写、写完关
            proc = subprocess.Popen(
                [*self._shell, ex.req.command],
                cwd=self._workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # setsid：自成一个进程组，pgid = shell 的 pid
            )
            with ex.cond:
                ex.pgid, ex.state = proc.pid, "running"
                ex.started_at = ex.last_activity = time.monotonic()
            line = short(ex.req.command)
            say("sandboxd", f"{ex.id} 开始：bash -lc '{line}'（进程组 {proc.pid}）")
            ex.emit({"t": "started", "pid": proc.pid, "boot_id": self.boot_id})
            reader = threading.Thread(target=self._pump, args=(ex, proc, log), daemon=True)
            reader.start()
            code = proc.wait()  # ★ 以 shell 退出为准
            reader.join(timeout=DETACH_S)
            if reader.is_alive() and not self.dead:
                say("sandboxd", f"{ex.id} shell 已退出，但后台进程还占着输出管道 → 不等它")
            with ex.cond:
                ex.detached = True
        # 屏障后半在释放并发槽之后等：写回慢不该挡住别的命令开始
        flushed = self._wait_flushed(ex) if self._barrier is not None else None
        self._finish(ex, code, flushed)

    def _wait_flushed(self, ex: Exec) -> bool:
        """★ 屏障后半：等写回队列清空，文件工具才读得到命令的产物。不设上限：查得到队列就一直等。"""
        assert self._barrier is not None
        last = 0
        while (n := self._barrier.queue()) > 0:
            if self.dead:
                return False
            if n != last:
                say("barrier", f"{ex.id} 已退出，等写回：还有 {n} 个文件没上传")
                ex.emit({"t": "flushing", "pending": n})
                last = n
            time.sleep(FLUSH_POLL_S)
        if last:
            say("barrier", f"{ex.id} 写回队列已清空")
        return True

    def _pump(self, ex: Exec, proc: subprocess.Popen[bytes], log: Any) -> None:
        assert proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        half = OUTPUT_LIMIT // 2
        while chunk := os.read(proc.stdout.fileno(), 65536):
            log.write(chunk)
            log.flush()
            if ex.detached:
                continue  # 命令已经结束了，这是后台进程的输出
            ex.total += len(chunk)
            ex.last_activity = time.monotonic()
            take = max(0, half - len(ex.head))
            ex.head += chunk[:take]
            ex.tail += chunk[take:]
            del ex.tail[: max(0, len(ex.tail) - half)]
            if text := decoder.decode(chunk):
                ex.emit({"t": "output", "data": text})
        proc.stdout.close()
        log.close()

    def _finish(self, ex: Exec, code: int, flushed: bool | None) -> None:
        truncated = ex.total > len(ex.head) + len(ex.tail)
        output = ex.head.decode(errors="replace")
        if truncated:
            omitted = ex.total - len(ex.head) - len(ex.tail)
            output += f"\n…（省略 {omitted} 字节；完整输出在沙箱内 {ex.log_path.name}）…\n"
        output += ex.tail.decode(errors="replace")
        sig = -code if code < 0 else None
        if sig:
            code = 128 + sig
        idle, timeout = ex.req.idle_s or IDLE_S, ex.req.timeout_s or 0
        notes = {
            "stalled": f"命令连续 {idle:g} 秒既无输出、也无 CPU 活动，判定为卡死，已结束",
            "timeout": f"命令超过指定的 {timeout:g} 秒，已结束",
            "killed": "命令已被取消",
        }
        reason = ex.kill_reason
        ex.emit(
            {
                "t": "exited",
                "exit_code": code,
                "signal": sig,
                "output": output,
                "output_bytes": ex.total,
                "truncated": truncated,
                "stalled": reason == "stalled",
                "timed_out": reason == "timeout",
                "killed": reason == "killed",
                "flushed": flushed,
                "notes": [notes[reason]] if reason else [],
            },
            final=True,
        )
        self.last_activity = time.monotonic()
        if self.dead:
            return  # 容器已经没了，没有人在这里说话
        why = f"（{reason}）" if reason else ""
        say("sandboxd", f"{ex.id} 结束：exit_code={code}{why}，输出 {ex.total} 字节")

    def _terminate(self, ex: Exec, reason: str) -> None:
        """★ 杀整个进程组：先 SIGTERM，宽限之后还在就 SIGKILL。"""
        with ex.cond:
            ex.kill_reason = ex.kill_reason or reason
            pgid = ex.pgid
        if pgid is None:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def hard() -> None:
            if ex.state != "exited":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)

        threading.Timer(KILL_GRACE_S, hard).start()

    def _watch(self) -> None:
        while not self._stop.wait(WATCH_S):
            with self._lock:
                running = [e for e in self._execs.values() if e.state == "running"]
            for ex in running:
                if ex.kill_reason is None:
                    self._check(ex, time.monotonic())
            if running:
                self.last_activity = time.monotonic()

    def _check(self, ex: Exec, now: float) -> None:
        if ex.req.timeout_s and ex.started_at and now - ex.started_at > ex.req.timeout_s:
            say("sandboxd", f"{ex.id} 超过模型指定的 {ex.req.timeout_s:g} 秒 → 结束进程组")
            self._terminate(ex, "timeout")
            return
        idle = ex.req.idle_s if ex.req.idle_s is not None else IDLE_S
        if not idle:
            return
        cpu = self.cpu(ex)
        if cpu is not None and cpu != ex.last_cpu:  # ★ CPU 在涨也算活着：安静的计算不是卡死
            ex.last_cpu = cpu
            ex.last_activity = now
        if now - ex.last_activity > idle:
            say("sandboxd", f"{ex.id} 连续 {idle:g} 秒没有输出、CPU 也没涨 → 判定卡死，结束进程组")
            self._terminate(ex, "stalled")


def group_cpu_seconds(pgid: int) -> float | None:
    """进程组的累计 CPU 秒数。

    Pod 里是 Linux，读 /proc；demo 在 macOS 上也要能跑，没有 /proc 就问 ps。
    """
    total = 0.0
    if os.path.isdir("/proc/self"):
        tick = os.sysconf("SC_CLK_TCK")
        for name in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    raw = f.read().decode(errors="replace")
            except OSError:
                continue
            fields = raw[raw.rfind(")") + 2 :].split()  # comm 可能含空格：从最后一个 ')' 后切
            if int(fields[2]) == pgid:  # pgrp；utime / stime 是 fields[11] / [12]
                total += (int(fields[11]) + int(fields[12])) / tick
        return total
    try:
        ps = subprocess.run(["ps", "-A", "-o", "pgid=,time="], capture_output=True, text=True)
    except OSError:
        return None
    for line in ps.stdout.splitlines():
        group, _, clock = line.strip().partition(" ")
        if group == str(pgid):
            seconds = 0.0
            for part in clock.strip().split(":"):  # macOS 是 M:SS.ss
                seconds = seconds * 60 + float(part)
            total += seconds
    return total


# ─────────────────────────────────────────────────────────────── HTTP 接口


class LinkFaults:
    """剧本往 server ↔ Pod 的网络里注入故障（真实 sandboxd 里没有这个）。"""

    def __init__(self) -> None:
        self.cut_after: dict[str, int] = {}  # exec_id → 事件流发到这个 seq 时断开一次


def serve(
    manager: ExecManager, *, token_file: Path, session_id: str, faults: LinkFaults
) -> ThreadingHTTPServer:
    token = token_file.read_text().strip()  # ★ 令牌是 Secret 卷里的文件，不是环境变量

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            # ★ per-Pod 令牌 + 会话绑定头。K8s 网络是平的，「在集群里」不是安全边界
            got = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(got, token):
                say("sandboxd", f"{self.command} {self.path}：令牌不对 → 401")
                self._json(401, {"error": "unauthorized"})
                return False
            if self.headers.get(SESSION_HEADER) != session_id:
                self._json(403, {"error": "session mismatch"})
                return False
            return True

        def _cut(self) -> None:
            """连接直接断掉：不发 chunked 的结尾。对端看到的是读到一半的流。"""
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

        def _stream(self, ex: Exec, after: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Sandbox-Boot-Id", manager.boot_id)
            self.end_headers()
            try:
                for event in ex.stream(after, lambda: manager.cpu(ex)):
                    if manager.dead:
                        self._cut()  # Pod 没了：连接断掉，不会有 exited
                        return
                    line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                    if event.get("seq", 0) >= faults.cut_after.get(ex.id, 1 << 30):
                        del faults.cut_after[ex.id]
                        say("剧本", f"{ex.id} 的事件流发到 seq {event['seq']} 时，网络断了一下")
                        self._cut()
                        return
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                say("sandboxd", f"{ex.id} 的事件流断开（对端不在了）→ 命令照跑")
                self.close_connection = True

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if urlsplit(self.path).path == "/v1/info":
                self._json(200, manager.info())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["v1", "execs"]:
                req = ExecRequest.parse(body)
                after = int(body.get("after") or 0)
                # ★ 调用方见过另一个 boot_id、这里又不认识这条命令 = Pod 重建过，原来那条已经没了。
                #   拒绝而不是再跑一遍：再跑会让副作用发生两次
                expect = str(body.get("expect_boot_id") or "")
                if expect and expect != manager.boot_id and manager.get(req.exec_id) is None:
                    say(
                        "sandboxd",
                        f"{req.exec_id}：对端见过 boot {expect}，我是 {manager.boot_id}，"
                        "也不认识这条命令 → 409，不重跑",
                    )
                    self._json(409, {"error": "boot changed", "boot_id": manager.boot_id})
                    return
                ex, created = manager.start(req)
                if not created:
                    say("sandboxd", f"{ex.id} 已存在（{ex.state}）→ 接上，不重跑（after={after}）")
                self._stream(ex, after)
            elif len(parts) == 4 and parts[:2] == ["v1", "execs"] and parts[3] == "kill":
                killed = manager.kill(parts[2])
                what = "向进程组发 SIGTERM" if killed else "已经结束了，无事可做"
                say("sandboxd", f"kill {parts[2]} → {what}")
                self._json(200, {"killed": killed})
            else:
                self._json(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="sandboxd-http", daemon=True).start()
    return server
