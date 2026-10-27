"""server 侧：工作区读写路由 —— FilesystemProtocol 的两套实现（设计 §9）。

    WorkspaceFilesystem（文件工具拿到的就是它）
      上层语义只有一份：路径归一与越界检查、「文件不存在」的文案、编码
      底层按路由表二选一：
        OssStore     工作区没有运行中的沙箱 → 直连对象存储
                     （多数会话一直是这样，不必为读个文件建 Pod）
        ExecdStore   沙箱在跑 → 经 execd 的文件 API 读写 Pod 里的 /workspace —— 与命令是同一个挂载

为什么沙箱在跑时要绕到沙箱里：对象存储和 Pod 的挂载是两个客户端，挂载对「别人的写入」有元数据缓存
（storage.py 的 CACHE_TTL_S）。文件工具若直连对象存储，它写的 count.py 在挂载里要过一会才看得到，
紧接着的 execute 就读不到（实验 3）。同一个挂载就只有一个缓存、一个写入顺序：
后台进程写的文件马上读得到，
交替写同一个文件也不会丢更新。

★ 只路由工作区路径；/skills 永远走对象存储（demo 省略）。
★ 故障：沙箱已经不在 → 删路由、改走对象存储重试；沙箱还在但 execd 不响应 → 读降级（注明），
  写不降级 —— 悄悄改写对象存储，就又多了一个写入方。
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from opensandbox_client import ExecdClient
from provision import SBX, Endpoint, Ensurer
from storage import ObjectStorage
from tracing import say

ROUTING = True  # 实验 3：改成 False，文件工具永远直连对象存储
T = TypeVar("T")


class PathEscape(ValueError):
    pass


def normalize(path: str) -> str:
    """模型给的 a.py、/workspace/a.py 都归到工作区内的相对路径。会话隔离的安全关键点（S12）。"""
    norm = posixpath.normpath(posixpath.join("/", path))
    if norm == "/workspace" or norm.startswith("/workspace/"):
        norm = norm[len("/workspace") :] or "/"
    if norm == "/":
        raise PathEscape(f"路径必须指向工作区里的文件：{path!r}")
    return norm.lstrip("/")


class OssStore:
    def __init__(self, storage: ObjectStorage, prefix: str) -> None:
        self._s, self._prefix = storage, prefix

    async def get(self, rel: str) -> bytes | None:
        return self._s.get(self._prefix + rel)

    async def put(self, rel: str, data: bytes) -> None:
        self._s.put(self._prefix + rel, data, origin="对象存储 API")


class ExecdStore:
    def __init__(self, ep: Endpoint, transport: httpx.AsyncBaseTransport) -> None:
        self._c = ExecdClient(ep.url, ep.token, transport)

    async def get(self, rel: str) -> bytes | None:
        return await self._c.download(f"/workspace/{rel}")

    async def put(self, rel: str, data: bytes) -> None:
        await self._c.upload(f"/workspace/{rel}", data)  # 返回时文件已 close → 已写穿


class WorkspaceFilesystem:
    def __init__(
        self, *, ws: str, oss: OssStore, ensurer: Ensurer, transport: httpx.AsyncBaseTransport
    ) -> None:
        self._ws, self._oss, self._e, self._transport = ws, oss, ensurer, transport

    async def aread(self, path: str) -> str:
        rel = normalize(path)
        data = await self._io("read", rel, lambda store: store.get(rel))
        return data.decode() if data is not None else f"Error: 文件不存在：{path}"

    async def awrite(self, path: str, content: str) -> str:
        rel = normalize(path)
        await self._io("write", rel, lambda store: store.put(rel, content.encode()))
        return f"已写入 {path}"

    async def _io(
        self, op: str, rel: str, fn: Callable[[OssStore | ExecdStore], Awaitable[T]]
    ) -> T:
        route = self._e.routes.get(self._ws) if ROUTING else None
        if route is None:
            say("路由", f"{op} {rel} → 对象存储（工作区没有运行中的沙箱）")
            return await fn(self._oss)
        try:
            say("路由", f"{op} {rel} → 沙箱 {route.sandbox_id}（与命令同一个挂载）")
            result = await fn(ExecdStore(route, self._transport))
            self._e.routes.touch(self._ws)  # 文件操作也算活动：闲置回收看它
            return result
        except httpx.TransportError:
            info = await self._e.servers[SBX].get(route.sandbox_id)
            state = info["status"]["state"] if info else "已删除"
            if state != "Running":
                say(
                    "路由",
                    f"沙箱 {route.sandbox_id} 已经不在（{state}）→ 删路由，{op} 改走对象存储重试",
                )
                self._e.forget(SBX, self._ws)
                return await fn(self._oss)
            if op == "read":
                say("路由", f"沙箱 {route.sandbox_id} 在，但 execd 不响应 → 这次读降级到对象存储")
                return await fn(self._oss)
            raise
