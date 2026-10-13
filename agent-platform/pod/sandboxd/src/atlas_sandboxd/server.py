"""sandboxd 的 HTTP 接口（doc/native-sandbox-design.html §6.2）。

    POST /v1/execs                    开始或接上一条命令（按 exec_id 幂等），流式返回 NDJSON 事件
                                      重连时带 after 与 expect_boot_id（守护进程重启过 → 409）
    GET  /v1/execs/{id}?after=N       只接上、不开始
    POST /v1/execs/{id}/kill          结束进程组
    POST /v1/execs/kill?run_id=…      结束某个 run 的全部命令
    GET  /v1/info                     boot_id、运行中的命令、闲置时长（cluster 闲置回收用）
    GET  /readyz  /healthz

★ 鉴权同 bridge：per-Pod 令牌（Secret 文件）+ 会话绑定头。K8s 网络是平的，「在集群里」不是安全边界。
★ 流断了命令照跑：客户端断开只是不再收事件，执行与结果都在守护进程里，接上即可取回。
"""

from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import ServeConfig
from .execs import ExecManager, ExecRequest

__all__ = ["make_server"]

logger = logging.getLogger(__name__)

SESSION_HEADER = "X-Atlas-Sandbox"


def _mounted(path: str) -> bool:
    try:
        with open("/proc/mounts") as f:
            return any(line.split()[1] == path for line in f if len(line.split()) > 1)
    except OSError:
        return False


def make_server(cfg: ServeConfig, manager: ExecManager) -> ThreadingHTTPServer:
    token = cfg.token()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            if not self.path.startswith(("/readyz", "/healthz")):
                logger.info("%s %s", self.command, self.path)

        # ------------------------------------------------------------ 工具

        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if token:
                got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if not hmac.compare_digest(got, token):
                    self._json(401, {"error": "unauthorized"})
                    return False
            if cfg.session_id and self.headers.get(SESSION_HEADER, "") != cfg.session_id:
                self._json(403, {"error": "session mismatch"})
                return False
            return True

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _stream(self, exec_id: str, after: int) -> None:
            ex = manager.get(exec_id)
            if ex is None:
                self._json(404, {"error": "exec not found", "boot_id": manager.boot_id})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Sandbox-Boot-Id", manager.boot_id)
            self.end_headers()
            try:
                for event in ex.stream(after, cfg.heartbeat_s, lambda: manager.cpu(ex)):
                    line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # 客户端走了：命令照跑，接上即可取回
                logger.info("exec %s 的事件流断开（命令继续）", exec_id)
                self.close_connection = True

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/healthz":
                self._json(200, {"status": "ok"})
                return
            if url.path == "/readyz":
                ok = not cfg.require_mount or _mounted(cfg.workspace)
                self._json(200 if ok else 503, {"ready": ok})
                return
            if not self._authorized():
                return
            if url.path == "/v1/info":
                self._json(200, manager.info())
                return
            parts = url.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["v1", "execs"]:
                after = int((parse_qs(url.query).get("after") or ["0"])[0])
                self._stream(parts[2], after)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            url = urlsplit(self.path)
            if not self._authorized():
                return
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            parts = url.path.strip("/").split("/")
            if url.path == "/v1/execs":
                try:
                    req = ExecRequest.parse(body)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                # ★ 重连时调用方带上它见过的 boot_id：守护进程已重启（Pod 重建）且不认识这个
                #   exec 时拒绝，而不是把命令再跑一遍 —— 那会让副作用发生两次
                expect = str(body.get("expect_boot_id") or "")
                if expect and expect != manager.boot_id and manager.get(req.exec_id) is None:
                    self._json(409, {"error": "boot changed", "boot_id": manager.boot_id})
                    return
                ex, _ = manager.start(req)
                self._stream(ex.id, int(body.get("after") or 0))
                return
            if url.path == "/v1/execs/kill":
                run_id = (parse_qs(url.query).get("run_id") or [""])[0]
                if not run_id:
                    self._json(400, {"error": "run_id 必填"})
                    return
                self._json(200, {"killed": manager.kill_run(run_id)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "execs"] and parts[3] == "kill":
                self._json(200, {"killed": manager.kill(parts[2])})
                return
            self._json(404, {"error": "not found"})

    server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    server.daemon_threads = True
    return server
