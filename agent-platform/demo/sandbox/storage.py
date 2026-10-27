"""基础设施（内存模拟）：对象存储、写穿式 CSI 挂载、Redis。

    文件工具（无沙箱）── 对象存储 API ──▶ ObjectStorage    {user}/{ws}/workspace/…
    Pod 里的 /workspace ── CsiMount（本地目录）── 写穿 / 元数据缓存 ──▶ 同一个前缀

CsiMount 模拟的是设计 §6 的「整桶静态 PVC + 按请求 subPath」，驱动是 s3fs 系（写穿式）：

    写  文件 close 时同步上传（写穿）。demo 用「每 20 毫秒扫一次本地目录
        + 命令退出 / 文件 API 写完时
        再刷一次」来近似 —— 所以工具结果返回时，产物已经在对象存储里
    读  别人（界面上传、别的 Pod）在对象存储上改了文件，挂载要等元数据缓存过期才看得到：
        demo 固定为 CACHE_TTL_S 之后可见（最坏情况），真实系统靠短缓存把它压到 1 秒左右

★ 对象存储凭据只在 CSI 驱动手里（真实：nodePublishSecretRef）。Pod 和 Atlas 都碰不到挂载器 ——
  这正是 Atlas 不能再「执行前 forget、执行后等写回」的原因，一致性改由路由（workspace.py）保证。
★ 真实的 CSI 挂载在节点上，同一节点的 Pod 可能共用一个；demo 简化成每个 Pod 一个。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tracing import say

SCAN_S = 0.02
CACHE_TTL_S = 0.6  # 外部写入在挂载里的可见延迟。真实目标 ≤ 1 秒（设计 §10）


@dataclass
class Obj:
    data: bytes
    version: int
    origin: str  # 谁写的：某个挂载、对象存储 API、界面上传


class ObjectStorage:
    """对象存储：所有 Pod、所有 server 实例共用，沙箱删了它还在。"""

    def __init__(self) -> None:
        self._objects: dict[str, Obj] = {}
        self._version = 0

    def put(self, key: str, data: bytes, *, origin: str) -> int:
        self._version += 1
        self._objects[key] = Obj(data, self._version, origin)
        return self._version

    def get(self, key: str) -> bytes | None:
        obj = self._objects.get(key)
        return obj.data if obj else None

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def under(self, prefix: str) -> dict[str, Obj]:
        """前缀下的全部对象，键是相对路径。"""
        return {k[len(prefix) :]: v for k, v in self._objects.items() if k.startswith(prefix)}


class CsiMount:
    """一个 Pod 里看到的 PVC subPath。挂载点是真实的本地目录，命令就在里面读写。"""

    def __init__(
        self,
        storage: ObjectStorage,
        prefix: str,
        local: Path,
        *,
        owner: str,
        read_only: bool = False,
    ) -> None:
        self.storage, self.prefix, self.local = storage, prefix, local
        self.owner, self.read_only = owner, read_only
        self._sig: dict[str, tuple[int, int]] = {}  # 本地文件上次同步时的 (mtime, size)
        self._seen: dict[str, int] = {}  # 本地内容对应的对象版本
        self._pending: dict[str, tuple[int, float]] = {}  # 外部改写：(版本, 何时可见)
        self._ticker: asyncio.Task[None] | None = None
        self._closed = False

    async def mount(self) -> None:
        """kubelet 启动容器前，CSI 节点插件就把挂载建好了。

        一个新挂载看到的就是对象存储的当前内容。
        """
        self.local.mkdir(parents=True, exist_ok=True)
        for rel, obj in self.storage.under(self.prefix).items():
            self._write_local(rel, obj)
        # 挂载活得比任何一个 run 都长：不继承创建它的那个 run 的上下文（否则日志的 run 标签会错）
        self._ticker = asyncio.create_task(self._tick(), context=contextvars.Context())

    async def unmount(self) -> None:
        """Pod 删除。写穿：已经 close 的文件都在对象存储里了，这里只是把最后一轮扫描补完。"""
        if self._ticker:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        self.flush()
        self._closed = True  # 卸载之后本地目录会被删掉：再 flush 会把「全没了」当成删除传上去

    def flush(self, *, background: bool = False) -> None:
        """把本地改动传上去（写穿的 close）。

        background = 后台扫描发现的（只报新文件，免得刷屏）。
        """
        if self.read_only or self._closed:
            return
        uploaded = []
        now = self._scan()
        for rel, sig in now.items():
            if self._sig.get(rel) != sig:
                new = rel not in self._sig
                data = (self.local / rel).read_bytes()
                self._seen[rel] = self.storage.put(self.prefix + rel, data, origin=self.owner)
                self._sig[rel] = sig
                if new or not background:
                    uploaded.append(rel)
        for rel in set(self._sig) - set(now):  # 本地删掉了
            self.storage.delete(self.prefix + rel)
            del self._sig[rel]
            self._seen.pop(rel, None)
            uploaded.append(f"{rel}（删除）")
        if uploaded:
            say("CSI 挂载", f"{self.owner} 写穿 → 对象存储：{'、'.join(uploaded)}")

    # ------------------------------------------------------------------ 内部

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(SCAN_S)
            self.flush(background=True)
            self._pull(time.monotonic())

    def _pull(self, now: float) -> None:
        """对象存储上别人写的新版本：元数据缓存过期之后才进挂载。"""
        for rel, obj in self.storage.under(self.prefix).items():
            if obj.origin == self.owner or self._seen.get(rel) == obj.version:
                continue
            pending = self._pending.get(rel)
            if pending is None or pending[0] != obj.version:
                self._pending[rel] = (obj.version, now + CACHE_TTL_S)
                say(
                    "CSI 挂载",
                    f"{rel} 在对象存储上被 {obj.origin} 改写 → "
                    f"{self.owner} 的挂载 {CACHE_TTL_S:g} 秒内"
                    "还是旧的（元数据缓存）",
                )
            elif now >= pending[1]:
                self._write_local(rel, obj)
                del self._pending[rel]
                say("CSI 挂载", f"缓存过期 → {self.owner} 的挂载里 {rel} 换成新版本")

    def _write_local(self, rel: str, obj: Obj) -> None:
        path = self.local / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(obj.data)
        st = path.stat()
        self._sig[rel] = (st.st_mtime_ns, st.st_size)
        self._seen[rel] = obj.version

    def _scan(self) -> dict[str, tuple[int, int]]:
        out = {}
        for path in self.local.rglob("*"):
            if path.is_file():
                st = path.stat()
                out[str(path.relative_to(self.local))] = (st.st_mtime_ns, st.st_size)
        return out


class FakeRedis:
    """Redis：路由表、端点缓存、令牌、活动记录、分布式锁。所有 server 实例共用。"""

    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.sets: dict[str, set[str]] = defaultdict(set)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def get(self, key: str) -> object | None:
        return self.kv.get(key)

    def set(self, key: str, value: object) -> None:
        self.kv[key] = value

    def delete(self, key: str) -> None:
        self.kv.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.kv if k.startswith(prefix))

    def lock(self, name: str) -> asyncio.Lock:
        """分布式锁的替身（真实：infra-platform/libs/redislock，设计 §13 借此接入）。"""
        return self._locks[name]
