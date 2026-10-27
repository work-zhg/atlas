"""Pod 里 execd 的薄客户端（doc/sandbox-opensandbox-design.html §8、§9）。

    ExecdClient   命令（异步）：后台提交 → init 事件里的命令 id；状态；游标日志；中断；指标
    ExecdFiles    文件（同步）：OssFilesystem 的同步核心跑在线程里，经它读写 Pod 里的 /workspace

★ 命令一律后台模式（设计 E2）：execd 的前台命令在连接断开时会被 SIGKILL（上游 runtime/command.go）；
  后台命令不随连接走，完成后输出保留 24 小时，断线后用同一个 id 和游标接着读。
★ 日志游标只用服务端返回的值（响应头 EXECD-COMMANDS-TAIL-CURSOR）：规范说是行号，实现是字节偏移。
★ 命令记录只在 execd 的内存里：execd 重启（Pod 重建、容器重启）后 status 返回 404。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

__all__ = ["TOKEN_HEADER", "ExecdClient", "ExecdFiles", "ExecdGone"]

TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
_TAIL_CURSOR = "EXECD-COMMANDS-TAIL-CURSOR"


class ExecdGone(Exception):
    """execd 不认识这个命令 id —— 它重启过（Pod 重建或容器重启），原来的命令已经不在了。"""


class ExecdClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            headers={TOKEN_HEADER: self._token},
        )

    async def start(self, command: str, *, cwd: str, timeout_s: int | None = None) -> str:
        """后台提交。读到 init 事件就返回命令 id —— 从这一刻起命令与这条连接无关。

        ★ 连接在 init 之前断开时，命令可能已经开始，但拿不到 id：抛 RemoteProtocolError，
          由调用方报「结果未知」，不重跑。
        """
        body: dict[str, Any] = {"command": command, "cwd": cwd, "background": True}
        if timeout_s:
            body["timeout"] = int(timeout_s) * 1000  # 毫秒；省略 = 不限（不设运行上限）
        async with (
            self._client() as client,
            client.stream("POST", f"{self.url}/command", json=body) as resp,
        ):
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")[:300]
                raise httpx.HTTPStatusError(
                    f"execd 返回 {resp.status_code}：{text}", request=resp.request, response=resp
                )
            async for line in resp.aiter_lines():
                event = _sse(line)
                if event is None:
                    continue
                if event.get("type") == "init":
                    return str(event["text"])
                if event.get("type") == "error":
                    raise httpx.HTTPStatusError(
                        f"execd 拒绝执行：{event}", request=resp.request, response=resp
                    )
        raise httpx.RemoteProtocolError("没收到 init 事件：命令可能已经开始，结果未知")

    async def logs(self, cmd_id: str, cursor: int) -> tuple[bytes, int]:
        """游标之后的全部输出 + 新游标。"""
        async with self._client() as client:
            resp = await client.get(f"{self.url}/command/{cmd_id}/logs", params={"cursor": cursor})
        if resp.status_code in (400, 404):
            raise ExecdGone(cmd_id)  # 不认识这个命令（execd 对未知 id 的日志请求回 400）
        resp.raise_for_status()
        return resp.content, int(resp.headers.get(_TAIL_CURSOR, cursor + len(resp.content)))

    async def status(self, cmd_id: str) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.get(f"{self.url}/command/status/{cmd_id}")
        if resp.status_code == 404:
            raise ExecdGone(cmd_id)
        resp.raise_for_status()
        return resp.json()

    async def interrupt(self, cmd_id: str) -> None:
        """SIGTERM 整个进程组，宽限后 SIGKILL（execd 侧）。"""
        async with self._client() as client:
            await client.delete(f"{self.url}/command", params={"id": cmd_id})

    async def cpu_pct(self) -> float | None:
        """容器级 CPU 使用率（execd 的 /metrics）。拿不到返回 None —— 卡死判定据此不下结论。"""
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.url}/metrics")
            resp.raise_for_status()
            return float(resp.json()["cpu_used_pct"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None


class ExecdFiles:
    """execd 的文件 API（同步）。路径是 Pod 里的绝对路径（/workspace/…）。"""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_s, transport=transport, trust_env=False, headers={TOKEN_HEADER: token}
        )

    def close(self) -> None:
        self._client.close()

    def size(self, path: str) -> int | None:
        """文件大小；不存在（或不是文件）返回 None。"""
        resp = self._client.get(f"{self.url}/files/info", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        info = (resp.json() or {}).get(path)
        if not info or info.get("type", "file") != "file":
            return None
        return int(info.get("size", 0))

    def download(self, path: str) -> bytes | None:
        resp = self._client.get(f"{self.url}/files/download", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def upload(self, path: str, data: bytes) -> None:
        """返回时文件已经 close —— 写穿式驱动下已经落盘（§9）。"""
        metadata = json.dumps({"path": path, "mode": 644})
        files = [
            ("metadata", ("metadata", metadata, "application/json")),
            ("file", (path.rsplit("/", 1)[-1] or "file", data, "application/octet-stream")),
        ]
        self._client.post(f"{self.url}/files/upload", files=files).raise_for_status()

    def search(self, path: str) -> list[dict[str, Any]]:
        """目录下（递归）的全部文件，元素含 path / size / type。目录不存在返回空。"""
        resp = self._client.get(f"{self.url}/files/search", params={"path": path, "pattern": "**"})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [f for f in resp.json() or [] if f.get("type", "file") == "file"]

    def remove(self, paths: list[str]) -> None:
        if paths:
            self._client.delete(
                f"{self.url}/files", params=[("path", p) for p in paths]
            ).raise_for_status()


def _sse(line: str) -> dict[str, Any] | None:
    """execd 的 SSE 事件。兼容带 `data:` 前缀与不带前缀（逐行 JSON）两种写法。"""
    line = line.strip()
    if not line:
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None
