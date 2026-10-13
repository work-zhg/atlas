"""文件一致性屏障（doc/native-sandbox-design.html §7）。

文件工具经对象存储 API 读写，命令经 rclone FUSE 读写同一前缀。两侧之间有缓存窗口，
实测（§7.1）外部改写过的文件经挂载读出来是「新内容按旧长度截断」—— 不是旧数据而是错数据。
所以在每次执行的边界上：

    执行前  vfs/forget（全部）       挂载器带 --attr-timeout 0，forget 之后立即读就是新的
    执行后  轮询 vfs/queue 直到为空   命令写的文件已上传，文件工具读得到

★ 为什么分成两个进程（§7.2）：rclone 的远程控制能调 operations/* 与 config/*，等于整桶凭据。
  同一 Pod 共享网络，命令也能连到它 —— 所以 rc 加口令，口令只给挂载器与本 sidecar（barrier），
  sandboxd 与命令所在的主容器拿不到。barrier 对外只有两个无害操作：让缓存失效、看队列长度。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import BarrierConfig

__all__ = ["BarrierClient", "FlushResult", "serve_barrier"]

logger = logging.getLogger(__name__)


def _post_json(url: str, body: dict[str, Any], *, auth: str = "", timeout: float = 10.0) -> Any:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    # 只连 Pod 内的 127.0.0.1
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _get_json(url: str, *, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


# ─────────────────────────────────────────────── sandboxd 侧的客户端


class FlushResult:
    def __init__(self, flushed: bool, note: str = "") -> None:
        self.flushed = flushed
        self.note = note


class BarrierClient:
    """sandboxd 用：执行前让缓存失效，执行后等写回。"""

    #: 屏障连续失败多少次就放弃等待（挂载器 / sidecar 坏了）。不是时间上限：只要能查到队列就一直等
    MAX_ERRORS = 10

    def __init__(
        self, url: str, *, poll_s: float = 0.2, sleep: Callable[[float], None] = time.sleep
    ):
        self._url = url.rstrip("/")
        self._poll = poll_s
        self._sleep = sleep

    def forget(self) -> str:
        """返回空串 = 成功；否则是给模型看的说明。失败不挡执行（命令照跑，结果里写明）。"""
        try:
            _post_json(f"{self._url}/forget", {"all": True})
            return ""
        except (OSError, ValueError) as exc:
            logger.warning("屏障 forget 失败：%s", exc)
            return f"工作区缓存刷新失败（{exc}），命令可能读到对象存储上几秒前的文件版本"

    def pending(self) -> int:
        return int(_get_json(f"{self._url}/queue").get("pending", 0))

    def wait_flushed(self, on_pending: Callable[[int], None] | None = None) -> FlushResult:
        errors = 0
        while True:
            try:
                n = self.pending()
                errors = 0
            except (OSError, ValueError) as exc:
                errors += 1
                if errors >= self.MAX_ERRORS:
                    return FlushResult(
                        False,
                        f"无法确认工作区写回完成（{exc}）；刚写的文件可能稍后才对文件工具可见",
                    )
                self._sleep(self._poll)
                continue
            if n == 0:
                return FlushResult(True)
            if on_pending is not None:
                on_pending(n)
            self._sleep(self._poll)


# ─────────────────────────────────────────────── barrier sidecar


class _Rc:
    def __init__(self, cfg: BarrierConfig) -> None:
        self._url = cfg.rc_url.rstrip("/")
        token = base64.b64encode(f"{cfg.rc_user}:{cfg.rc_pass}".encode()).decode()
        self._auth = f"Basic {token}" if cfg.rc_pass else ""

    def call(self, command: str, params: dict[str, Any] | None = None) -> Any:
        return _post_json(f"{self._url}/{command}", params or {}, auth=self._auth)


def serve_barrier(cfg: BarrierConfig) -> None:
    rc = _Rc(cfg)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 探针每秒一次，不打日志
            pass

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
            elif self.path == "/queue":
                try:
                    queue = rc.call("vfs/queue").get("queue") or []
                except (OSError, ValueError) as exc:
                    self._reply(502, {"error": str(exc)})
                    return
                self._reply(200, {"pending": len(queue)})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if self.path != "/forget":
                self._reply(404, {"error": "not found"})
                return
            try:
                # ★ 只做 forget：不转发任何其它 rc 命令，参数也不透传（防止借道调 operations/*）
                if body.get("all", True):
                    rc.call("vfs/forget")
                else:
                    for f in body.get("files") or []:
                        rc.call("vfs/forget", {"file": str(f)})
                    for d in body.get("dirs") or []:
                        rc.call("vfs/forget", {"dir": str(d)})
            except (OSError, ValueError) as exc:
                self._reply(502, {"error": str(exc)})
                return
            self._reply(200, {"ok": True})

    # ★ 只监听回环：本来就只有同 Pod 的 sandboxd 用
    server = ThreadingHTTPServer(("127.0.0.1", cfg.port), Handler)
    server.daemon_threads = True
    logger.info("barrier 监听 127.0.0.1:%d → rclone rc %s", cfg.port, cfg.rc_url)
    server.serve_forever()
