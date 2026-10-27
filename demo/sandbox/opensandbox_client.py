"""server 侧：OpenSandbox 的两个薄客户端（设计 §18 的 providers/opensandbox/）。

    LifecycleClient  lifecycle server：建 / 查 / 列 / 删沙箱、解析端点、写 Credential Vault
                     （带 API key）
    ExecdClient      Pod 里的 execd：后台命令、状态、游标日志、中断、文件（带 execd 令牌）

★ 不用官方 SDK（设计 D10）：一共十几个接口，而命令必须用「后台 + 轮询」—— SDK 的 run() 走前台 SSE，
  前台命令在连接断开时会被 execd SIGKILL（execd.py 的 _foreground，实验 1）。
★ 每个请求一个 AsyncClient，transport 由调用方给（demo 里是 opensandbox.py 的「集群网络」）。
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx

API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"
EXECD_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
TAIL_CURSOR_HEADER = "EXECD-COMMANDS-TAIL-CURSOR"


class LifecycleClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport) -> None:
        self.base_url, self._key, self._transport = base_url, api_key, transport

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        async with httpx.AsyncClient(transport=self._transport, timeout=10) as client:
            return await client.request(
                method, f"{self.base_url}/v1{path}", headers={API_KEY_HEADER: self._key}, **kw
            )

    async def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        """server 会等到 Running 才返回（设计 §2：默认最长 60 秒，Atlas 调到 180）。"""
        resp = await self._call("POST", "/sandboxes", json=spec)
        resp.raise_for_status()
        return resp.json()

    async def list(self, metadata: dict[str, str]) -> list[dict[str, Any]]:
        """按 metadata 过滤 —— 按会话复用、配额、孤儿回收都靠它（metadata 会成为 Pod 的 label）。"""
        query = urllib.parse.quote(urllib.parse.urlencode(metadata))
        resp = await self._call("GET", f"/sandboxes?metadata={query}")
        resp.raise_for_status()
        return resp.json()["items"]

    async def get(self, sandbox_id: str) -> dict[str, Any] | None:
        resp = await self._call("GET", f"/sandboxes/{sandbox_id}")
        return None if resp.status_code == 404 else resp.json()

    async def delete(self, sandbox_id: str) -> None:
        await self._call("DELETE", f"/sandboxes/{sandbox_id}")

    async def endpoint(self, sandbox_id: str, port: int) -> str:
        resp = await self._call("GET", f"/sandboxes/{sandbox_id}/endpoints/{port}")
        resp.raise_for_status()
        return resp.json()["endpoint"]

    async def credential_vault(self, sandbox_id: str, body: dict[str, Any]) -> None:
        resp = await self._call("POST", f"/sandboxes/{sandbox_id}/credential-vault", json=body)
        resp.raise_for_status()


class ExecdClient:
    def __init__(self, url: str, token: str, transport: httpx.AsyncBaseTransport) -> None:
        self.url, self._token, self._transport = url, token, transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=10, headers={EXECD_TOKEN_HEADER: self._token}
        )

    # ------------------------------------------------------------------ 命令

    async def start(self, command: str, *, cwd: str, timeout_s: int | None = None) -> str:
        """后台模式提交：读 SSE 读到 init 事件就拿到命令 id，命令从此和这条连接无关。"""
        body: dict[str, Any] = {"command": command, "cwd": cwd, "background": True}
        if timeout_s:
            body["timeout"] = timeout_s * 1000  # execd 的 timeout 是毫秒，省略 = 不限
        async with (
            self._client() as client,
            client.stream("POST", f"{self.url}/command", json=body) as resp,
        ):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                event = _sse(line)
                if event and event["type"] == "init":
                    return event["text"]
        raise httpx.RemoteProtocolError("没收到 init 事件：命令可能已开始，结果未知")

    async def run_foreground(
        self, command: str, *, cwd: str, timeout_s: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """前台模式（实验 1 才用）：输出跟着这条 SSE 连接走，连接一断命令就被杀。"""
        body: dict[str, Any] = {"command": command, "cwd": cwd, "background": False}
        if timeout_s:
            body["timeout"] = timeout_s * 1000
        async with (
            self._client() as client,
            client.stream("POST", f"{self.url}/command", json=body) as resp,
        ):
            async for line in resp.aiter_lines():
                if event := _sse(line):
                    yield event

    async def logs(self, cmd_id: str, cursor: int) -> tuple[bytes, int]:
        """游标之后的全部输出 + 新游标。

        ★ 游标只用服务端返回的值（规范说是行号，实现是字节偏移）。
        """
        async with self._client() as client:
            resp = await client.get(f"{self.url}/command/{cmd_id}/logs", params={"cursor": cursor})
        resp.raise_for_status()
        return resp.content, int(resp.headers[TAIL_CURSOR_HEADER])

    async def status(self, cmd_id: str) -> dict[str, Any] | None:
        """None = execd 不认识这个 id（它重启过：命令记录只在内存里）。"""
        async with self._client() as client:
            resp = await client.get(f"{self.url}/command/status/{cmd_id}")
        return None if resp.status_code == 404 else resp.json()

    async def interrupt(self, cmd_id: str) -> None:
        async with self._client() as client:
            await client.delete(f"{self.url}/command", params={"id": cmd_id})

    # ------------------------------------------------------------------ 文件

    async def download(self, path: str) -> bytes | None:
        async with self._client() as client:
            resp = await client.get(f"{self.url}/files/download", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    async def upload(self, path: str, data: bytes) -> None:
        """返回时文件已 close → 已写穿。

        ★ 真实是 multipart（metadata part + file part）；demo 简化成 JSON。
        """
        async with self._client() as client:
            resp = await client.post(
                f"{self.url}/files/upload", json={"path": path, "content": data.decode()}
            )
        resp.raise_for_status()


def _sse(line: str) -> dict[str, Any] | None:
    return json.loads(line[5:]) if line.startswith("data:") else None
