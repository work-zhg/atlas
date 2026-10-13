"""命令执行（doc/native-sandbox-design.html §6.3 / §11）。

★ exec_id 幂等（D7）：同一个 id 再来一次不重跑，而是接上原来那条命令 —— server 重启、
  网络抖动后重放同一个 tool_call 时，副作用不会发生两次。
★ 不设时长上限（D6）：只做静默卡死判定 —— 连续 idle_s 秒既无输出、进程组 CPU 时间也没变化，
  才结束它。模型显式给的 timeout 照办（那是模型自己的决定）。
★ 每条命令一个新会话（setsid），结束时杀整个进程组 —— 只杀 shell 会留下孤儿。
  命令里 & / nohup 起的后台进程会自己 setsid 或被 bash 放进同一组：前者不受影响（可以起服务），
  后者随命令结束被清理是 bash 的默认行为，不额外处理。
★ stdin 是 /dev/null：等输入的命令（apt 的确认、git 的口令提示）立即失败而不是永远挂着。
★ 以 shell 退出为「命令结束」，不以输出管道 EOF 为准：`cmd &` 起的后台进程继承了 stdout，
  管道会一直开着。shell 退出后再等 0.5 秒收尾，之后读线程只把后台进程的输出写进日志文件。
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .barrier import BarrierClient
from .config import ServeConfig
from .procfs import group_cpu_seconds

__all__ = ["Exec", "ExecManager", "ExecRequest"]

logger = logging.getLogger(__name__)


@dataclass
class ExecRequest:
    exec_id: str
    command: str
    run_id: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: 模型显式给的超时（秒）；None = 不限
    timeout_s: float | None = None
    #: 静默卡死判定（秒）；None = 用守护进程默认，0 = 不判定
    idle_s: float | None = None

    @classmethod
    def parse(cls, body: dict[str, Any]) -> ExecRequest:
        exec_id = str(body.get("exec_id") or "").strip()
        command = body.get("command")
        if not exec_id or len(exec_id) > 200:
            raise ValueError("exec_id 必填且不超过 200 字符")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command 必填")
        env = body.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("env 必须是对象")
        timeout = body.get("timeout_s")
        idle = body.get("idle_s")
        return cls(
            exec_id=exec_id,
            command=command,
            run_id=str(body.get("run_id") or ""),
            cwd=body.get("cwd") or None,
            env={str(k): str(v) for k, v in env.items()},
            timeout_s=float(timeout) if timeout else None,
            idle_s=float(idle) if idle is not None else None,
        )


class Exec:
    def __init__(self, req: ExecRequest, log_path: Path) -> None:
        self.req = req
        self.id = req.exec_id
        self.log_path = log_path
        self.state = "queued"  # queued | running | exited
        self.created = time.monotonic()
        self.started_at: float | None = None
        self.ended: float | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.pgid: int | None = None
        self.events: list[dict[str, Any]] = []
        self._seq = 0
        self._replay_bytes = 0
        self.cond = threading.Condition()
        # 输出：头尾各留一半给模型，完整的在日志文件里
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.last_activity = time.monotonic()
        self.last_cpu: float | None = None
        self.kill_reason: str | None = None  # stalled | timeout | killed
        self.result: dict[str, Any] | None = None
        #: shell 已退出、结果已定：读线程此后只写日志（后台进程的输出）
        self.detached = False

    # ------------------------------------------------------------------ 事件

    def emit(self, event: dict[str, Any], *, replay_limit: int) -> None:
        with self.cond:
            self._seq += 1
            event = {"seq": self._seq, **event}
            self.events.append(event)
            if event["t"] == "output":
                self._replay_bytes += len(event["data"])
                # 只裁输出事件，started / exited 永远保留
                while self._replay_bytes > replay_limit:
                    idx = next((i for i, e in enumerate(self.events) if e["t"] == "output"), None)
                    if idx is None:
                        break
                    self._replay_bytes -= len(self.events.pop(idx)["data"])
            self.cond.notify_all()

    def stream(
        self, after: int, heartbeat_s: float, cpu: Callable[[], float | None]
    ) -> Iterator[dict[str, Any]]:
        """从 seq > after 开始给事件，直到 exited。等待期间每 heartbeat_s 发一次心跳（不入库）。"""
        while True:
            with self.cond:
                pending = [e for e in self.events if e["seq"] > after]
                if not pending and self.state != "exited":
                    self.cond.wait(timeout=heartbeat_s)
                    pending = [e for e in self.events if e["seq"] > after]
                done = self.state == "exited"
            if pending:
                for e in pending:
                    yield e
                    after = e["seq"]
                    if e["t"] == "exited":
                        return
            elif done:
                return
            else:
                yield {
                    "t": "heartbeat",
                    "state": self.state,
                    "cpu_s": cpu(),
                    "output_bytes": self.total,
                }

    def summary(self) -> dict[str, Any]:
        return {
            "exec_id": self.id,
            "run_id": self.req.run_id,
            "state": self.state,
            "output_bytes": self.total,
            "age_s": round(time.monotonic() - self.created, 1),
        }


class ExecManager:
    def __init__(
        self,
        cfg: ServeConfig,
        *,
        barrier: BarrierClient | None = None,
        cpu_of: Callable[[int], float | None] = group_cpu_seconds,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.boot_id = uuid.uuid4().hex
        self._barrier = barrier
        self._cpu_of = cpu_of
        self._clock = clock
        self._execs: dict[str, Exec] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(cfg.max_concurrency)
        self._log_dir = Path(cfg.log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        #: 最近一次「有命令在跑或刚结束」的时间 —— cluster 的闲置回收看它
        self.last_activity = clock()
        self._shell = ["bash", "-lc"] if shutil.which("bash") else ["sh", "-c"]
        self._stop = threading.Event()
        threading.Thread(target=self._watch, name="sandboxd-watch", daemon=True).start()

    # ------------------------------------------------------------------ 对外

    def start(self, req: ExecRequest) -> tuple[Exec, bool]:
        """返回 (exec, 是否新建)。同一 exec_id 已存在 → 接上原来那条，不重跑。"""
        with self._lock:
            existing = self._execs.get(req.exec_id)
            if existing is not None:
                return existing, False
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in req.exec_id)
            ex = Exec(req, self._log_dir / f"{safe}.log")
            self._execs[req.exec_id] = ex
        threading.Thread(target=self._run, args=(ex,), name=f"exec-{safe}", daemon=True).start()
        return ex, True

    def get(self, exec_id: str) -> Exec | None:
        with self._lock:
            return self._execs.get(exec_id)

    def kill(self, exec_id: str, reason: str = "killed") -> bool:
        ex = self.get(exec_id)
        if ex is None or ex.state == "exited":
            return False
        self._terminate(ex, reason)
        return True

    def kill_run(self, run_id: str) -> list[str]:
        with self._lock:
            targets = [
                e for e in self._execs.values() if e.req.run_id == run_id and e.state != "exited"
            ]
        for ex in targets:
            self._terminate(ex, "killed")
        return [e.id for e in targets]

    def info(self) -> dict[str, Any]:
        with self._lock:
            execs = [e.summary() for e in self._execs.values()]
        running = [e for e in execs if e["state"] != "exited"]
        return {
            "boot_id": self.boot_id,
            "running": len(running),
            # 有命令在跑时「闲置时长」恒为 0：cluster 永远不回收正在干活的沙箱（不设运行上限）
            "idle_s": 0.0 if running else round(self._clock() - self.last_activity, 1),
            "execs": execs,
        }

    def cpu(self, ex: Exec) -> float | None:
        return self._cpu_of(ex.pgid) if ex.pgid else None

    def close(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 执行

    def _run(self, ex: Exec) -> None:
        limit = self.cfg.replay_limit_bytes
        with self._slots:  # 并发上限（并行子智能体共用一个沙箱）
            self.last_activity = self._clock()
            notes: list[str] = []
            if self._barrier is not None and (note := self._barrier.forget()):
                notes.append(note)
            cwd = ex.req.cwd or self.cfg.workspace
            if not os.path.isdir(cwd):
                self._finish(ex, exit_code=None, started=None, notes=[f"工作目录不存在：{cwd}"])
                return
            # 守护进程自己的配置不进命令环境（调用方传的同名变量也不行）
            deny = self.cfg.env_deny_prefixes
            env = {k: v for k, v in os.environ.items() if not k.startswith(deny)}
            env.update({k: v for k, v in ex.req.env.items() if not k.startswith(deny)})
            started = self._clock()
            log = open(ex.log_path, "wb")  # noqa: SIM115 — 由读线程写、结束时关
            try:
                # 执行任意命令正是沙箱的职责
                proc = subprocess.Popen(
                    [*self._shell, ex.req.command],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                log.close()
                self._finish(ex, exit_code=None, started=started, notes=[f"无法启动命令：{exc}"])
                return
            with ex.cond:
                ex.proc, ex.pgid, ex.state = proc, proc.pid, "running"
                ex.started_at = ex.last_activity = self._clock()
            ex.emit({"t": "started", "pid": proc.pid, "boot_id": self.boot_id}, replay_limit=limit)
            reader = threading.Thread(target=self._pump, args=(ex, proc, log), daemon=True)
            reader.start()
            code = proc.wait()
            reader.join(timeout=0.5)
            with ex.cond:
                ex.detached = True
        # 屏障在释放并发槽之后等：写回慢不该挡住别的命令开始
        flushed = None
        if self._barrier is not None:
            res = self._barrier.wait_flushed(
                lambda n: ex.emit({"t": "flushing", "pending": n}, replay_limit=limit)
            )
            flushed = res.flushed
            if res.note:
                notes.append(res.note)
        self._finish(ex, exit_code=code, started=started, notes=notes, flushed=flushed)

    def _pump(self, ex: Exec, proc: subprocess.Popen[bytes], log: Any) -> None:
        assert proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        half = self.cfg.output_limit_bytes // 2
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            log.write(chunk)
            log.flush()
            if ex.detached:
                continue
            ex.total += len(chunk)
            ex.last_activity = self._clock()
            if len(ex.head) < half:
                take = half - len(ex.head)
                ex.head += chunk[:take]
                chunk_rest = chunk[take:]
            else:
                chunk_rest = chunk
            if chunk_rest:
                ex.tail += chunk_rest
                if len(ex.tail) > half:
                    del ex.tail[: len(ex.tail) - half]
            text = decoder.decode(chunk)
            if text:
                ex.emit({"t": "output", "data": text}, replay_limit=self.cfg.replay_limit_bytes)
        proc.stdout.close()
        log.close()

    def _finish(
        self,
        ex: Exec,
        *,
        exit_code: int | None,
        started: float | None,
        notes: list[str],
        flushed: bool | None = None,
    ) -> None:
        now = self._clock()
        truncated = ex.total > len(ex.head) + len(ex.tail)
        if truncated:
            omitted = ex.total - len(ex.head) - len(ex.tail)
            output = (
                ex.head.decode(errors="replace")
                + f"\n…（省略 {omitted} 字节；完整输出在沙箱内 {ex.log_path}）…\n"
                + ex.tail.decode(errors="replace")
            )
        else:
            output = (ex.head + ex.tail).decode(errors="replace")
        sig = None
        if exit_code is not None and exit_code < 0:
            sig, exit_code = -exit_code, 128 - exit_code
        reason = ex.kill_reason
        if reason == "stalled":
            idle = ex.req.idle_s if ex.req.idle_s is not None else self.cfg.idle_s
            notes.append(f"命令连续 {int(idle)} 秒既无输出、也无 CPU 活动，判定为卡死，已结束")
        elif reason == "timeout":
            notes.append(f"命令超过指定的 {int(ex.req.timeout_s or 0)} 秒，已结束")
        elif reason == "killed":
            notes.append("命令已被取消")
        result = {
            "t": "exited",
            "exit_code": exit_code,
            "signal": sig,
            "duration_ms": int((now - started) * 1000) if started is not None else 0,
            "output": output,
            "output_bytes": ex.total,
            "truncated": truncated,
            "stalled": reason == "stalled",
            "timed_out": reason == "timeout",
            "killed": reason == "killed",
            "flushed": flushed,
            "notes": notes,
            "log_path": str(ex.log_path),
        }
        with ex.cond:
            ex.state = "exited"
            ex.ended = now
            ex.result = result
        self.last_activity = now
        ex.emit(result, replay_limit=self.cfg.replay_limit_bytes)

    def _terminate(self, ex: Exec, reason: str) -> None:
        with ex.cond:
            if ex.kill_reason is None:
                ex.kill_reason = reason
            pgid = ex.pgid
        if pgid is None:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def _hard() -> None:
            if ex.state != "exited":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)

        threading.Timer(self.cfg.kill_grace_s, _hard).start()

    # ------------------------------------------------------------------ 巡检

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            now = self._clock()
            with self._lock:
                execs = list(self._execs.values())
            for ex in execs:
                if ex.state == "running" and ex.kill_reason is None:
                    self._check(ex, now)
                elif ex.state == "exited" and ex.ended and now - ex.ended > self.cfg.retention_s:
                    with self._lock:
                        self._execs.pop(ex.id, None)
                    ex.log_path.unlink(missing_ok=True)
            if any(e.state != "exited" for e in execs):
                self.last_activity = now

    def _check(self, ex: Exec, now: float) -> None:
        # 超时从真正开始执行算起（排队等并发槽的时间不算）
        if (
            ex.req.timeout_s
            and ex.started_at is not None
            and now - ex.started_at > ex.req.timeout_s
        ):
            self._terminate(ex, "timeout")
            return
        idle = ex.req.idle_s if ex.req.idle_s is not None else self.cfg.idle_s
        if not idle:
            return
        cpu = self.cpu(ex)
        if cpu is not None and cpu != ex.last_cpu:
            ex.last_cpu = cpu
            ex.last_activity = now
        if now - ex.last_activity > idle:
            logger.info("exec %s 判定卡死（%.0fs 无输出且无 CPU 活动）", ex.id, idle)
            self._terminate(ex, "stalled")
