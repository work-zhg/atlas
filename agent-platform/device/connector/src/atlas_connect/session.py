"""本机会话（c4-model/agent/connector/code-connector-session.html）。

    SessionHost       设备网关开的会话流：核对本机策略 → 解析项目目录 → 确保 bridge → 接上
    ProjectDir        项目目录：展开 ~、解析符号链接，必须在允许的目录里
    BridgeSupervisor  按会话管 bridge：在跑就复用，不在就拉起（python -m atlas_bridge），闲置停掉
    splice            把会话流接到 bridge：原样对转，只看一眼 turn.start 的权限模式

★ bridge 就是会话 Pod 里的那个程序：计时、取消、CLI 重启、断线补发都由它处理。
★ bridge 只监听本机回环，令牌每次拉起随机生成：别的电脑连不到，本机别的程序没有令牌也接不上。
★ 启动什么命令由本机配置决定（按 cli_type 取），平台给不了。环境变量按白名单，再加上模型接入。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas_devicelink import INTERNAL_ERROR, NORMAL, Channel, ChannelClosed
from atlas_host import SUBPROTOCOL_V1, CloseCode, methods
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .audit import AuditLog
from .config import BASE_ENV, LocalPolicy, Paths, Rejection

__all__ = ["BridgeSupervisor", "ProjectDir", "Rejected", "SessionHost"]

logger = logging.getLogger(__name__)

#: 本机拒绝：不可重连并带原因，原路回到网页
DEVICE_REJECTED = CloseCode.DEVICE_REJECTED
SUPERSEDED = CloseCode.SUPERSEDED
SESSION_GONE = CloseCode.SESSION_GONE
SUBPROTOCOL = SUBPROTOCOL_V1
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
BRIDGE_READY_TIMEOUT_S = 120.0


class Rejected(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

    @classmethod
    def of(cls, rejection: Rejection) -> Rejected:
        return cls(rejection.reason, rejection.message)


class ProjectDir:
    @staticmethod
    def resolve(raw: str, policy: LocalPolicy, *, create: bool) -> Path:
        """展开 ~、解析符号链接，结果必须在允许的目录内。

        不存在：create=True（默认目录）就建，否则拒绝。
        """
        if not raw.strip():
            raise Rejected("dir_not_allowed", "没有给项目目录")
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        real = path.resolve()
        if not policy.within_roots(real):
            roots = "、".join(policy.allowed_roots) or "（没有配置）"
            raise Rejected(
                "dir_not_allowed",
                f"目录 {raw} 不在这台电脑允许的范围内（{roots}）。"
                "要用它，先在电脑上把它加进本机策略的 allowed_roots",
            )
        if not real.exists():
            if not create:
                raise Rejected("dir_not_found", f"这台电脑上没有目录 {raw}")
            real.mkdir(parents=True, exist_ok=True)
        if not real.is_dir():
            raise Rejected("dir_not_found", f"{raw} 不是目录")
        return real


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class BridgeProcess:
    thread: str
    cli_type: str
    project: Path
    port: int
    token: str
    proc: asyncio.subprocess.Process
    log: Path
    started_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.monotonic)
    splices: int = 0

    def alive(self) -> bool:
        return self.proc.returncode is None

    async def stop(self, grace_s: float = 10.0) -> None:
        """SIGTERM：bridge 排空（当前一轮以 pod_terminating 收尾、关掉 CLI）后退出。"""
        if not self.alive():
            return
        with contextlib.suppress(ProcessLookupError):
            self.proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()

    def log_tail(self, n: int = 20) -> str:
        with contextlib.suppress(OSError):
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])
        return ""


class BridgeStartFailed(Exception):
    pass


class BridgeSupervisor:
    def __init__(self, policy: LocalPolicy, paths: Paths) -> None:
        self._policy = policy
        self._paths = paths
        self._bridges: dict[str, BridgeProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def running(self) -> list[BridgeProcess]:
        return [b for b in self._bridges.values() if b.alive()]

    def get(self, thread: str) -> BridgeProcess | None:
        bridge = self._bridges.get(thread)
        return bridge if bridge is not None and bridge.alive() else None

    async def ensure(self, launch: dict[str, Any], project: Path) -> BridgeProcess:
        thread = str(launch["thread"])
        async with self._locks.setdefault(thread, asyncio.Lock()):
            bridge = self.get(thread)
            if bridge is not None:
                return bridge
            bridge = await self._launch(launch, project)
            self._bridges[thread] = bridge
            return bridge

    async def _launch(self, launch: dict[str, Any], project: Path) -> BridgeProcess:
        thread, cli_type = str(launch["thread"]), str(launch["cli_type"])
        command = self._policy.clis[cli_type].command
        self._paths.ensure()
        token = secrets.token_urlsafe(32)
        token_file = self._paths.run / f"{thread}.token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        port = _free_port()
        agent_env, allow = _agent_env(launch.get("model_env"), self._policy)
        env = {k: v for k, v in os.environ.items() if k in allow}
        env |= agent_env
        env |= {
            "ATLAS_BRIDGE_SESSION_ID": thread,
            "ATLAS_BRIDGE_TOKEN_FILE": str(token_file),
            "ATLAS_BRIDGE_LISTEN_HOST": "127.0.0.1",
            "ATLAS_BRIDGE_LISTEN_PORT": str(port),
            "ATLAS_BRIDGE_AGENT_CMD": json.dumps(list(command)),
            "ATLAS_BRIDGE_WORKSPACE": str(project),
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": ",".join(sorted(allow | set(agent_env))),
            # 本机：不切换用户、不调 OOM 优先级
            "ATLAS_BRIDGE_AGENT_UID": "",
            "ATLAS_BRIDGE_AGENT_GID": "",
            "ATLAS_BRIDGE_AGENT_OOM_SCORE_ADJ": "",
        }
        log = self._paths.run / f"{thread}.log"
        with log.open("ab") as out:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "atlas_bridge",
                cwd=str(project),
                env=env,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        (self._paths.run / f"{thread}.pid").write_text(str(proc.pid))
        bridge = BridgeProcess(thread, cli_type, project, port, token, proc, log)
        try:
            await _await_ready(bridge)
        except BridgeStartFailed:
            await bridge.stop(grace_s=2)
            raise
        logger.info("会话 %s 的 bridge 已就绪（pid %s，端口 %s）", thread, proc.pid, port)
        return bridge

    async def stop(self, thread: str) -> bool:
        bridge = self._bridges.pop(thread, None)
        if bridge is None:
            return False
        await bridge.stop()
        _cleanup(self._paths, thread)
        return True

    async def stop_all(self) -> None:
        for thread in list(self._bridges):
            await self.stop(thread)

    async def reap_idle(self) -> list[str]:
        """闲置超过 idle_stop_s、没有接着会话流的 bridge 停掉；自己退出的清掉记录。"""
        now, stopped = time.monotonic(), []
        for thread, bridge in list(self._bridges.items()):
            if not bridge.alive():
                self._bridges.pop(thread, None)
                _cleanup(self._paths, thread)
                continue
            if bridge.splices == 0 and now - bridge.last_active > self._policy.idle_stop_s:
                await self.stop(thread)
                stopped.append(thread)
        return stopped

    def kill_stale(self) -> None:
        """连接器上次异常退出留下的 bridge：按 pid 文件停掉（启动时调用）。"""
        if not self._paths.run.exists():
            return
        for pid_file in self._paths.run.glob("*.pid"):
            with contextlib.suppress(ValueError, ProcessLookupError, PermissionError, OSError):
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            _cleanup(self._paths, pid_file.stem)


def _cleanup(paths: Paths, thread: str) -> None:
    for suffix in (".pid", ".token"):
        with contextlib.suppress(OSError):
            (paths.run / f"{thread}{suffix}").unlink()


def _agent_env(
    model_env: dict[str, Any] | None, policy: LocalPolicy
) -> tuple[dict[str, str], set[str]]:
    """→ (要额外设的变量, 允许从连接器环境继承的变量名)。

    平台的模型：指向设备网关上的地址，凭证是会话令牌。本机登录（没有 model_env）：不设模型变量，
    CLI 用电脑上自己的登录；这时才允许继承电脑上原有的 ANTHROPIC_API_KEY 之类。
    """
    allow = set(BASE_ENV) | set(policy.env_allow)
    if not model_env:
        allow |= {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "OPENAI_API_KEY",
        }
        return {}, allow
    model, small = str(model_env["model"]), str(model_env.get("small_model") or model_env["model"])
    env = {
        "ANTHROPIC_BASE_URL": str(model_env["base_url"]),
        "ANTHROPIC_AUTH_TOKEN": str(model_env["token"]),
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": small,
        # 新版 Claude Code 按别名找模型：不指的话它会用内置的模型名，网关里没有
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
    }
    if not model_env.get("prompt_cache", True):
        env["DISABLE_PROMPT_CACHING"] = "1"
    if model_env.get("headers"):
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(
            f"{k}: {v}" for k, v in model_env["headers"].items()
        )
    return env, allow


async def _probe(port: int) -> tuple[int, str | None]:
    """GET /readyz → (状态码, X-Session-Id)。连不上返回 (0, None)。"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
    except (OSError, TimeoutError):
        return 0, None
    try:
        writer.write(b"GET /readyz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), 2)
    except (OSError, TimeoutError):
        return 0, None
    finally:
        writer.close()
    lines = raw.decode(errors="replace").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, None
    session = next(
        (
            line.split(":", 1)[1].strip()
            for line in lines[1:]
            if line.lower().startswith("x-session-id:")
        ),
        None,
    )
    return status, session


async def _await_ready(bridge: BridgeProcess) -> None:
    """/readyz 回 200（拉起 CLI 并完成初始化）。bridge 或 CLI 起不来时进程退出，不用等满时限。"""
    deadline = time.monotonic() + BRIDGE_READY_TIMEOUT_S
    while True:
        if not bridge.alive():
            raise BridgeStartFailed(
                f"bridge 退出了（{bridge.proc.returncode}）：\n{bridge.log_tail()}"
            )
        status, session = await _probe(bridge.port)
        if status == 200 and session == bridge.thread:
            return
        if time.monotonic() > deadline:
            raise BridgeStartFailed(f"bridge 在时限内没有就绪：\n{bridge.log_tail()}")
        await asyncio.sleep(0.3)


async def splice(channel: Channel, bridge: BridgeProcess, policy: LocalPolicy) -> None:
    """把会话流接到 bridge：原样对转，只看一眼 turn.start 的权限模式；任何一端结束就关另一端。"""
    try:
        ws = await connect(
            f"ws://127.0.0.1:{bridge.port}/host",
            additional_headers={
                "Authorization": f"Bearer {bridge.token}",
                "X-Session-Id": bridge.thread,
            },
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
            compression=None,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=None,
            proxy=None,
        )
    except Exception as exc:
        await channel.close(INTERNAL_ERROR, f"接不上本机的 bridge：{exc}")
        return
    bridge.splices += 1
    rejected: list[Rejection] = []

    async def from_platform() -> None:
        async for message in channel:
            if isinstance(message, str) and (r := _mode_check(message, policy)) is not None:
                rejected.append(r)
                return
            bridge.last_active = time.monotonic()
            await ws.send(message)

    async def from_bridge() -> None:
        with contextlib.suppress(ConnectionClosed):
            async for message in ws:
                bridge.last_active = time.monotonic()
                await channel.send(message)

    up, down = asyncio.create_task(from_platform()), asyncio.create_task(from_bridge())
    try:
        done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (up, down):
            task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        bridge.splices -= 1
        bridge.last_active = time.monotonic()
    if rejected:
        await ws.close()
        await reject(channel, rejected[0].reason, rejected[0].message)
        return
    if up in done:
        # 平台那边断了：bridge 照常运行，等下一条会话流接上（attach、补发）
        await ws.close()
        return
    await _close_like_bridge(channel, ws)


async def _close_like_bridge(channel: Channel, ws: ClientConnection) -> None:
    code = ws.close_code
    reason = ws.close_reason or ""
    if code in (SESSION_GONE, SUPERSEDED, NORMAL):
        await channel.close(code, reason)
    else:
        await channel.close(INTERNAL_ERROR, reason or "本机的 bridge 断开了")


async def reject(channel: Channel, reason: str, message: str) -> None:
    """本机拒绝：先把完整原因作为一条通知发给 Agent 服务，再以 4403 关闭。

    关闭原因最多只能有 123 字节，装不下完整原因。
    """
    notice = {
        "jsonrpc": "2.0",
        "method": methods.DEVICE_REJECTED,
        "params": {"reason": reason, "message": message},
    }
    with contextlib.suppress(ChannelClosed):
        await channel.send(json.dumps(notice, ensure_ascii=False))
    await channel.close(DEVICE_REJECTED, message[:1000])


def _mode_check(message: str, policy: LocalPolicy) -> Rejection | None:
    """只看 turn.start：本机的权限模式上限优先。其余消息不解读。"""
    try:
        obj = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("method") != "turn.start":
        return None
    params = obj.get("params") or {}
    mode = params.get("mode") if isinstance(params, dict) else None
    return policy.mode_rejection(mode if isinstance(mode, str) else None)


class SessionHost:
    def __init__(self, policy: LocalPolicy, paths: Paths, audit: AuditLog) -> None:
        self._policy = policy
        self._audit = audit
        self.bridges = BridgeSupervisor(policy, paths)

    async def on_session(self, channel: Channel) -> None:
        launch = channel.head
        thread = str(launch.get("thread") or "")
        try:
            existing = self.bridges.get(thread)
            if existing is None:
                rejection = self._policy.check_open(launch, running=len(self.bridges.running))
                if rejection is not None:
                    raise Rejected.of(rejection)
                project = ProjectDir.resolve(
                    str(launch.get("project_dir") or ""),
                    self._policy,
                    create=bool(launch.get("create_dir")),
                )
                try:
                    bridge = await self.bridges.ensure(launch, project)
                except BridgeStartFailed as exc:
                    cli = self._policy.clis[str(launch["cli_type"])]
                    hint = (
                        f"。检查：atlas-connect doctor；安装：{cli.install}" if cli.install else ""
                    )
                    raise Rejected(
                        "cli_failed", f"{launch['cli_type']} 在这台电脑上起不来{hint}\n{exc}"
                    ) from exc
                self._audit.record("开会话", f"{thread}  {launch.get('cli_type')}  {project}")
            else:
                bridge = existing
        except Rejected as exc:
            self._audit.record("拒绝开会话", f"{thread}  {exc.message.splitlines()[0]}")
            await reject(channel, exc.reason, exc.message)
            return
        with contextlib.suppress(ChannelClosed):
            await splice(channel, bridge, self._policy)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "thread": b.thread,
                "cli_type": b.cli_type,
                "project": str(b.project),
                "pid": b.proc.pid,
                "started_at": b.started_at,
                "attached": b.splices > 0,
            }
            for b in self.bridges.running
        ]

    async def stop(self, prefix: str) -> list[str]:
        """按会话 id 前缀结束会话（bridge 排空后退出，这一轮在网页上显示为结束）。"""
        matched = [b.thread for b in self.bridges.running if b.thread.startswith(prefix)]
        for thread in matched:
            await self.bridges.stop(thread)
            self._audit.record("结束会话", f"{thread}（在本机结束）")
        return matched

    async def shutdown(self) -> None:
        await self.bridges.stop_all()
