"""进程内的假 OpenSandbox：lifecycle API + 每个沙箱一个 execd。

doc/sandbox-opensandbox-design.html §8、§9。

    lifecycle   http://osb/v1/sandboxes…   建 / 列（metadata 过滤、分页）/ 查 / 删 / 端点
    execd       http://{id}.execd:44772/…  后台命令（真跑 bash）、游标日志、状态、中断、指标、
                                           文件 API
    bridge      http://{id}.execd:8900/readyz  ACP Pod 的 bridge 就绪（可设成先回几次 503）
    egress      http://{id}.execd:18080/…      Credential Vault（要 OPENSANDBOX-EGRESS-AUTH）

挂载的模拟：同一个工作区的所有沙箱共用 root/{ws} 这个目录（= 同一个 subPath 的写穿挂载），
命令的 cwd 与文件 API 里的 /workspace 都换算到它。

★ 只模拟 Atlas 依赖的那部分语义，且尽量照上游实现：未知命令 id 的日志请求回 400、状态回 404；
  SSE 是逐行 JSON（没有 data: 前缀）；execd 重启后命令记录全丢。
★ 故障注入：break_execd(n) 让接下来 n 个 execd 请求连不上；restart_execd 清空命令记录；
  vanish 让沙箱从 lifecycle 里消失（Pod 被删 / 驱逐）。
"""

from __future__ import annotations

import asyncio
import contextlib
import email.parser
import email.policy
import itertools
import json
import os
import signal
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from pydantic import SecretStr

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from atlas_agent.config import Settings

LIFECYCLE_URL = "http://osb"
EXECD_PORT = 44772
BRIDGE_PORT = 8900
EGRESS_PORT = 18080
_CURSOR = "EXECD-COMMANDS-TAIL-CURSOR"


@dataclass
class _Command:
    proc: subprocess.Popen[bytes]
    log: Path


@dataclass
class FakeSandbox:
    id: str
    spec: dict[str, Any]
    state: str
    created_at: datetime
    workspace: Path
    #: 还要被 GET 几次才离开 Pending
    pending_gets: int = 0
    commands: dict[str, _Command] = field(default_factory=dict)
    #: bridge 的 /readyz 还要回几次 503
    bridge_unready: int = 0
    #: egress sidecar 内存里的 Credential Vault（None = 还没建）
    vault: dict[str, Any] | None = None
    vault_posts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def metadata(self) -> dict[str, str]:
        return dict(self.spec.get("metadata") or {})

    @property
    def token(self) -> str:
        return str((self.spec.get("env") or {}).get("EXECD_ACCESS_TOKEN", ""))

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": {"state": self.state},
            "metadata": self.metadata,
            "createdAt": self.created_at.isoformat().replace("+00:00", "Z"),
        }


class FakeOpenSandbox:
    def __init__(self, root: Path, *, host: str = "osb") -> None:
        """host：lifecycle 的主机名。评测档的用例另起一个（osb-bench）当第二个 server。"""
        self.root = root
        self.host = host
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.created: list[FakeSandbox] = []
        self.deleted: list[str] = []
        #: 下一次创建的结果状态；Pending 时还要被 GET pending_gets 次才变 Running
        self.create_state = "Running"
        self.pending_gets = 0
        #: 创建耗时（只在异步 transport 上生效：让并发的 ensure 真的交错）
        self.create_delay_s = 0.0
        #: 提交命令时，命令已经开始、init 事件晚这么久才回来（异步 transport 上生效）
        self.submit_delay_s = 0.0
        self.cpu_pct: float | None = 50.0
        #: 新建的沙箱，bridge 的 /readyz 先回几次 503
        self.bridge_unready = 0
        self.drop_init = False
        self.page_size = 100
        self._broken = 0
        #: 沙箱 id 带随机前缀：Redis 里的令牌键以它为后缀，用例之间不能撞
        self.prefix = f"sbx{uuid4().hex[:6]}"
        self._ids = itertools.count(1)
        self.async_transport = httpx.MockTransport(self._handle_async)
        self.sync_transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------ 测试用的操纵

    def add(
        self, spec: dict[str, Any], *, state: str = "Running", age_s: float = 0.0
    ) -> FakeSandbox:
        """不经 Atlas 直接放一个沙箱进去（别的进程建的、崩溃留下的）。"""
        ws = (spec.get("metadata") or {}).get("atlas.thread", "none")
        sb = FakeSandbox(
            id=f"{self.prefix}-{next(self._ids)}",
            spec=spec,
            state=state,
            created_at=datetime.fromtimestamp(datetime.now(UTC).timestamp() - age_s, UTC),
            workspace=self.workspace(ws),
            pending_gets=self.pending_gets if state == "Pending" else 0,
            bridge_unready=self.bridge_unready,
        )
        self.sandboxes[sb.id] = sb
        return sb

    def workspace(self, ws: str) -> Path:
        path = self.root / ws
        path.mkdir(parents=True, exist_ok=True)
        return path

    def break_execd(self, n: int) -> None:
        self._broken = n

    def restart_execd(self, sandbox_id: str) -> None:
        """容器重启：execd 的命令记录全丢（命令本身随容器一起没了）。"""
        sb = self.sandboxes[sandbox_id]
        for cmd in sb.commands.values():
            _kill(cmd.proc)
        sb.commands.clear()

    def vanish(self, sandbox_id: str) -> None:
        """沙箱被删 / 驱逐：lifecycle 里没了，它的 execd 也连不上了。"""
        sb = self.sandboxes.pop(sandbox_id)
        for cmd in sb.commands.values():
            _kill(cmd.proc)

    def running(self) -> list[FakeSandbox]:
        return [sb for sb in self.sandboxes.values() if sb.state == "Running"]

    def close(self) -> None:
        for sb in self.sandboxes.values():
            for cmd in sb.commands.values():
                _kill(cmd.proc)

    # ------------------------------------------------------------------ 路由

    async def _handle_async(self, request: httpx.Request) -> httpx.Response:
        if self.create_delay_s and request.method == "POST" and request.url.host == self.host:
            await asyncio.sleep(self.create_delay_s)
        response = self._handle(request)
        if self.submit_delay_s and request.method == "POST" and request.url.path == "/command":
            await asyncio.sleep(self.submit_delay_s)
        return response

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == self.host:
            return self._lifecycle(request)
        if request.url.host.endswith(".execd"):
            return self._execd(request)
        raise httpx.ConnectError(f"unknown host {request.url.host}", request=request)

    # ------------------------------------------------------------------ lifecycle

    def _lifecycle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/sandboxes" and method == "POST":
            spec = json.loads(request.content)
            sb = self.add(spec, state=self.create_state)
            self.created.append(sb)
            return httpx.Response(202, json=sb.view())
        if path == "/v1/sandboxes" and method == "GET":
            return self._list(request)
        parts = path.removeprefix("/v1/sandboxes/").split("/")
        sb = self.sandboxes.get(parts[0])
        if sb is None:
            return httpx.Response(404, json={"message": f"sandbox {parts[0]} not found"})
        if len(parts) == 1 and method == "GET":
            if sb.state == "Pending":
                sb.pending_gets -= 1
                if sb.pending_gets <= 0:
                    sb.state = "Running"
            return httpx.Response(200, json=sb.view())
        if len(parts) == 1 and method == "DELETE":
            self.vanish(sb.id)
            self.deleted.append(sb.id)
            return httpx.Response(204)
        if len(parts) == 3 and parts[1] == "endpoints":
            body: dict[str, Any] = {"endpoint": f"{sb.id}.execd:{parts[2]}"}
            if int(parts[2]) == EGRESS_PORT:
                body["headers"] = {"OPENSANDBOX-EGRESS-AUTH": f"egress-{sb.id}"}
            return httpx.Response(200, json=body)
        return httpx.Response(405)

    def _list(self, request: httpx.Request) -> httpx.Response:
        raw = urllib.parse.unquote(request.url.params.get("metadata", ""))
        want = dict(urllib.parse.parse_qsl(raw))
        page = int(request.url.params.get("page", "1"))
        match = [
            sb.view()
            for sb in self.sandboxes.values()
            if all(sb.metadata.get(k) == v for k, v in want.items())
        ]
        start = (page - 1) * self.page_size
        body = {
            "items": match[start : start + self.page_size],
            "pagination": {"page": page, "hasNextPage": start + self.page_size < len(match)},
        }
        return httpx.Response(200, json=body)

    # ------------------------------------------------------------------ execd

    def _execd(self, request: httpx.Request) -> httpx.Response:
        if self._broken > 0:
            self._broken -= 1
            raise httpx.ConnectError("connection refused", request=request)
        sb = self.sandboxes.get(request.url.host.removesuffix(".execd"))
        if sb is None or sb.state not in ("Running", "Allocated"):  # Allocated：有 IP，可以接请求
            raise httpx.ConnectError("no route to host", request=request)
        if request.url.port == BRIDGE_PORT:
            return self._bridge(sb, request)
        if request.url.port == EGRESS_PORT:
            return self._egress(sb, request)
        if request.headers.get("X-EXECD-ACCESS-TOKEN") != sb.token:
            return httpx.Response(401, json={"message": "bad token"})
        path, method, params = request.url.path, request.method, request.url.params
        if path == "/command" and method == "POST":
            return self._start(sb, json.loads(request.content))
        if path == "/command" and method == "DELETE":
            cmd = sb.commands.get(params.get("id", ""))
            if cmd:
                _kill(cmd.proc)
            return httpx.Response(200 if cmd else 404)
        if path.startswith("/command/status/"):
            cmd = sb.commands.get(path.rsplit("/", 1)[-1])
            if cmd is None:
                return httpx.Response(404, json={"message": "command not found"})
            code = cmd.proc.poll()
            return httpx.Response(
                200, json={"running": code is None, "exit_code": code, "error": ""}
            )
        if path.startswith("/command/") and path.endswith("/logs"):
            cmd = sb.commands.get(path.split("/")[2])
            if cmd is None:
                return httpx.Response(400, json={"message": "invalid command id"})
            data = cmd.log.read_bytes()
            cursor = int(params.get("cursor", "0"))
            return httpx.Response(200, content=data[cursor:], headers={_CURSOR: str(len(data))})
        if path == "/metrics":
            if self.cpu_pct is None:
                return httpx.Response(500)
            return httpx.Response(200, json={"cpu_used_pct": self.cpu_pct})
        if path.startswith("/files"):
            return self._files(sb, request)
        return httpx.Response(404)

    @staticmethod
    def _bridge(sb: FakeSandbox, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/readyz":
            return httpx.Response(404)
        if sb.bridge_unready > 0:
            sb.bridge_unready -= 1
            return httpx.Response(503)
        return httpx.Response(200)

    @staticmethod
    def _egress(sb: FakeSandbox, request: httpx.Request) -> httpx.Response:
        if "credentialProxy" not in sb.spec:
            raise httpx.ConnectError("没有 egress sidecar", request=request)
        if request.headers.get("OPENSANDBOX-EGRESS-AUTH") != f"egress-{sb.id}":
            return httpx.Response(401)
        if request.url.path != "/credential-vault":
            return httpx.Response(404)
        if request.method == "GET":
            if sb.vault is None:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"revision": 1, "credentials": [], "bindings": sb.vault["bindings"]}
            )
        if request.method == "POST":
            if sb.vault is not None:
                return httpx.Response(409)
            body = json.loads(request.content)
            sb.vault_posts.append(body)
            sb.vault = body
            return httpx.Response(201, json={"revision": 1})
        return httpx.Response(405)

    def _start(self, sb: FakeSandbox, body: dict[str, Any]) -> httpx.Response:
        assert body.get("background") is True, "命令必须后台提交（前台命令断线即被杀）"
        cmd_id = f"cmd-{next(self._ids)}"
        log = self.root / f"{cmd_id}.log"
        with log.open("wb") as out:
            proc = subprocess.Popen(
                ["bash", "-c", body["command"]],
                cwd=self._local(sb, body.get("cwd") or "/workspace"),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        sb.commands[cmd_id] = _Command(proc, log)
        if self.drop_init:
            return httpx.Response(200, content=b"")
        events = [{"type": "init", "text": cmd_id}, {"type": "ping"}]
        return httpx.Response(200, content="".join(json.dumps(e) + "\n\n" for e in events).encode())

    def _files(self, sb: FakeSandbox, request: httpx.Request) -> httpx.Response:
        path, method, params = request.url.path, request.method, request.url.params
        if path == "/files/info":
            pod_path = params["path"]
            local = self._local(sb, pod_path)
            if not local.exists():
                return httpx.Response(404)
            kind = "file" if local.is_file() else "directory"
            size = local.stat().st_size if kind == "file" else 0
            return httpx.Response(
                200, json={pod_path: {"path": pod_path, "size": size, "type": kind}}
            )
        if path == "/files/download":
            local = self._local(sb, params["path"])
            if not local.is_file():
                return httpx.Response(404)
            return httpx.Response(200, content=local.read_bytes())
        if path == "/files/upload":
            meta, data = _multipart(request)
            local = self._local(sb, meta["path"])
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            return httpx.Response(200)
        if path == "/files/search":
            base = self._local(sb, params["path"])
            if not base.is_dir():
                return httpx.Response(404)
            found = [
                {"path": self._pod(sb, p), "size": p.stat().st_size, "type": "file"}
                for p in sorted(base.rglob("*"))
                if p.is_file()
            ]
            return httpx.Response(200, json=found)
        if path == "/files" and method == "DELETE":
            for pod_path in params.get_list("path"):
                self._local(sb, pod_path).unlink(missing_ok=True)
            return httpx.Response(200)
        return httpx.Response(404)

    @staticmethod
    def _local(sb: FakeSandbox, pod_path: str) -> Path:
        assert pod_path == "/workspace" or pod_path.startswith("/workspace/"), pod_path
        return sb.workspace / pod_path.removeprefix("/workspace").lstrip("/")

    @staticmethod
    def _pod(sb: FakeSandbox, local: Path) -> str:
        return "/workspace/" + local.relative_to(sb.workspace).as_posix()


def _multipart(request: httpx.Request) -> tuple[dict[str, Any], bytes]:
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + request.content)
    parts = {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in msg.iter_parts()
    }
    return json.loads(parts["metadata"]), parts["file"]


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


# ─────────────────────────────────────────────────────────────── 用例共用


def fake_settings(**overrides: Any) -> Settings:
    """指向假 lifecycle 的配置；路由缓存关掉（用例直接改 Redis，要立刻看得到）。"""
    from atlas_agent.config import get_settings

    base: dict[str, Any] = {
        "sandbox_enabled": True,
        "opensandbox_sbx_url": LIFECYCLE_URL,
        "opensandbox_sbx_api_key": SecretStr("test-key"),
        # 两个实例指向同一个假 server：配额按 base_url 去重，只数一次
        "opensandbox_acp_url": LIFECYCLE_URL,
        "opensandbox_acp_api_key": SecretStr("test-key"),
        "opensandbox_timeout_s": 5.0,
        "sandbox_route_cache_s": 0.0,
    }
    return get_settings().model_copy(update=base | overrides)


async def wipe(redis: aioredis.Redis, *needles: str) -> None:
    """删掉本用例留下的 atlas:sbx:* / atlas:acp:* 键（会话 id、沙箱 id 前缀都可以当 needle）。"""
    keys = [
        k
        for pattern in ("atlas:sbx:*", "atlas:acp:*")
        async for k in redis.scan_iter(match=pattern)
        if any(n in k for n in needles)
    ]
    if keys:
        await redis.delete(*keys)
