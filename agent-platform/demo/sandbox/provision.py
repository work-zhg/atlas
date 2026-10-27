"""server 侧：沙箱的供给与回收 —— cluster 下线后，它的职责搬到这里（设计 §13、§14）。

    SandboxRoutes  Redis 路由表 sbx:route:{ws}（有记录 = 工作区有运行中的沙箱，文件工具走沙箱）
                   + 活动记录（运行中的命令、最近活动时间），闲置回收用
    Ensurer        ensure(kind, thread)：按 metadata 复用 → 会话级锁内再查一次
                   → 配额 / 镜像校验 → 创建
                   → 解析端点 → 写缓存（原生沙箱同时写路由）
    Maintainer     闲置回收（先删路由、再删沙箱）、孤儿回收、会话删除

★ 创建请求不能由客户端指定 id，并发的 ensure 会建出两个沙箱 —— 所以要锁（实验 2）。
★ 令牌由 Atlas 生成、经 env 交给沙箱；GET /sandboxes 不会把 env 还回来，所以令牌必须由 Atlas 自己存
  （demo：Redis 的 token:{sandbox_id}）。存丢了就连不上，只能删掉重建（README §10 问题 1）。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opensandbox_client import LifecycleClient
from storage import FakeRedis
from tracing import say

SBX, ACP = "sbx", "acp"
EXECD_PORT, BRIDGE_PORT = 44772, 8900
PVC = "atlas-oss"
QUOTA_PER_USER = 3  # 两种沙箱合计（原 cluster 的 quota_per_user）
IMAGE_ALLOW_PREFIXES = ("atlas-sandbox:", "atlas-acp:")
IDLE_TTL_S = 0.5  # 原生沙箱闲置多久回收（真实 30 分钟）


@dataclass(frozen=True)
class Endpoint:
    sandbox_id: str
    url: str
    token: str


class QuotaExceeded(RuntimeError):
    pass


class ImageDenied(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────── 路由表


class SandboxRoutes:
    def __init__(self, redis: FakeRedis) -> None:
        self._r = redis

    def get(self, ws: str) -> Endpoint | None:
        return self._r.get(f"sbx:route:{ws}")  # type: ignore[return-value]

    def set(self, ws: str, ep: Endpoint) -> None:
        self._r.set(f"sbx:route:{ws}", ep)
        self.touch(ws)

    def delete(self, ws: str) -> None:
        self._r.delete(f"sbx:route:{ws}")

    def workspaces(self) -> list[str]:
        return [k.removeprefix("sbx:route:") for k in self._r.keys("sbx:route:")]

    def running(self, ws: str) -> set[str]:
        return self._r.sets[f"sbx:running:{ws}"]

    def touch(self, ws: str) -> None:
        self._r.set(f"sbx:active:{ws}", time.monotonic())

    def idle_for(self, ws: str) -> float:
        return time.monotonic() - float(self._r.get(f"sbx:active:{ws}") or 0)


# ─────────────────────────────────────────────────────────────── 创建请求


def _volume(name: str, sub_path: str, mount: str, *, read_only: bool = False) -> dict[str, Any]:
    """整桶 PVC + 按会话的 subPath（设计 §6）。前缀布局与 OssFilesystem 一致。"""
    return {
        "name": name,
        "pvc": {"claimName": PVC, "createIfNotExists": False},
        "mountPath": mount,
        "subPath": sub_path,
        "readOnly": read_only,
    }


def sbx_spec(*, ws: str, user: str, token: str, image: str) -> dict[str, Any]:
    return {
        "image": {"uri": image},
        "entrypoint": ["tail", "-f", "/dev/null"],
        "timeout": None,  # 不自动过期：回收由 Maintainer 做（不设运行上限）
        "resourceLimits": {"cpu": "2", "memory": "4Gi"},
        "env": {
            "EXECD_ACCESS_TOKEN": token,
            "EXECD_INIT": "1",
            "HOME": "/home/agent",
            "TMPDIR": "/tmp",
        },
        "metadata": {
            "atlas.managed": "true",
            "atlas.kind": SBX,
            "atlas.thread": ws,
            "atlas.user": user,
        },
        "volumes": [
            _volume("ws", f"{user}/{ws}/workspace", "/workspace"),
            _volume("skills", f"{user}/{ws}/skills", "/skills", read_only=True),
        ],
    }


def acp_spec(
    *, thread: str, ws: str, user: str, token: str, image: str, adapter: str, git: bool
) -> dict[str, Any]:
    base_env = ["HOME", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]
    spec: dict[str, Any] = {
        "image": {"uri": image},
        "entrypoint": ["python", "-m", "atlas_bridge"],
        "timeout": None,
        "env": {
            "ATLAS_BRIDGE_SESSION_ID": thread,
            "ATLAS_BRIDGE_TOKEN": token,  # 原来是 per-Pod Secret 文件
            "ATLAS_BRIDGE_LISTEN_PORT": str(BRIDGE_PORT),
            "ATLAS_BRIDGE_AGENT_CMD": adapter,
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": ",".join(base_env),
            "ATLAS_BRIDGE_AGENT_ENV_FILES": "ANTHROPIC_AUTH_TOKEN=/run/secrets/atlas/model_api_key",
            "HOME": "/state",
            "ANTHROPIC_BASE_URL": "http://llm-gateway",
            "ANTHROPIC_MODEL": "claude-sonnet-5",
        },
        "metadata": {
            "atlas.managed": "true",
            "atlas.kind": ACP,
            "atlas.thread": thread,
            "atlas.user": user,
        },
        "volumes": [
            _volume("ws", f"{user}/{ws}/workspace", "/workspace"),  # 父会话的工作区（共享）
            _volume("skills", f"{user}/{thread}/skills", "/skills", read_only=True),  # 自己的
            _volume(
                "state", f"{user}/{thread}/system/cli-home", "/state"
            ),  # CLI 的 HOME，resume 靠它
        ],
    }
    if git:  # Credential Vault 需要 egress sidecar：带一个策略并打开透明代理
        spec["networkPolicy"] = {"defaultAction": "allow"}
        spec["credentialProxy"] = {"enabled": True}
    return spec


# ─────────────────────────────────────────────────────────────── ensure


class Ensurer:
    def __init__(
        self, redis: FakeRedis, servers: dict[str, LifecycleClient], routes: SandboxRoutes
    ) -> None:
        self._r, self.servers, self.routes = redis, servers, routes

    async def ensure(
        self,
        kind: str,
        *,
        thread: str,
        user: str,
        image: str,
        spec: Callable[[str], dict[str, Any]],
        port: int,
        fresh: bool = False,
        after_create: Callable[[str], Awaitable[None]] | None = None,
    ) -> Endpoint:
        key = f"ep:{kind}:{thread}"
        if not fresh and (ep := self._r.get(key)):
            return ep  # type: ignore[return-value]
        async with self._r.lock(f"ensure:{kind}:{thread}"):  # 实验 2：去掉它
            if not fresh and (ep := self._r.get(key)):
                return ep  # type: ignore[return-value]  # 刚被并发的另一个 ensure 建好
            client = self.servers[kind]
            current = self._r.get(key)  # fresh 时它可能已经不可用，但仍是「上次用的那个」
            prefer = current.sandbox_id if isinstance(current, Endpoint) else None
            sandbox_id = await self._reusable(client, kind, thread, prefer)
            if sandbox_id is None:
                await self._admit(user, image)
                token = secrets.token_hex(8)
                created = await client.create(spec(token))
                sandbox_id = created["id"]
                self._r.set(f"token:{sandbox_id}", token)
                if after_create:
                    await after_create(sandbox_id)
                say("ensure", f"{kind} 会话 {thread}：没有可用的沙箱 → 建了 {sandbox_id}")
            else:
                say("ensure", f"{kind} 会话 {thread}：按 metadata 找到运行中的 {sandbox_id} → 复用")
            address = await client.endpoint(sandbox_id, port)
            ep = Endpoint(sandbox_id, f"http://{address}", str(self._r.get(f"token:{sandbox_id}")))
            self._r.set(key, ep)
            if kind == SBX:
                previous = self.routes.get(thread)
                self.routes.set(thread, ep)  # ★ 从这一刻起，这个工作区的文件工具走沙箱
                if previous != ep:
                    say("ensure", f"路由表 sbx:route:{thread} → {sandbox_id}（文件工具从此走沙箱）")
            return ep

    def forget(self, kind: str, thread: str) -> None:
        self._r.delete(f"ep:{kind}:{thread}")
        if kind == SBX:
            self.routes.delete(thread)

    async def _reusable(
        self, client: LifecycleClient, kind: str, thread: str, prefer: str | None
    ) -> str | None:
        """同一会话的沙箱里挑一个能用的：优先上次用的那个，其余的删掉。

        ★ 正常只有一个；多出来的（锁失效时并发建的）不只是浪费 —— 两个挂载就是两个客户端，
          断线重连还可能接到另一个沙箱上（实验 2），所以顺手删掉。
        """
        usable: list[dict] = []
        for sb in await client.list({"atlas.kind": kind, "atlas.thread": thread}):
            state = sb["status"]["state"]
            if state == "Pending":  # 有人正在建（或建到一半的进程崩了）：等它，不要删
                state = await self._wait_running(client, sb["id"])
            if state == "Running" and self._r.get(f"token:{sb['id']}"):
                usable.append(sb)
                continue
            why = "令牌不在（建它的进程没来得及存）" if state == "Running" else state
            say("ensure", f"{sb['id']} 是 {why}，用不了（restartPolicy: Never）→ 删掉重建")
            await client.delete(sb["id"])
        if not usable:
            return None
        usable.sort(key=lambda sb: (sb["id"] != prefer, -sb["createdAt"]))  # 上次用的 > 最新的
        for extra in usable[1:]:
            say("ensure", f"会话 {thread} 有多个沙箱：留 {usable[0]['id']}，删掉 {extra['id']}")
            await client.delete(extra["id"])
        return usable[0]["id"]

    @staticmethod
    async def _wait_running(client: LifecycleClient, sandbox_id: str, timeout: float = 5) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sb = await client.get(sandbox_id)
            state = sb["status"]["state"] if sb else "已删除"
            if state != "Pending":
                return state
            await asyncio.sleep(0.02)
        return "Pending（超时）"

    async def _admit(self, user: str, image: str) -> None:
        """配额（两种沙箱合计）与镜像把关 —— 原来在 cluster。★ 先数再建：软上限。"""
        if not image.startswith(IMAGE_ALLOW_PREFIXES):
            raise ImageDenied(f"镜像 {image!r} 不在允许范围内")
        count = sum([len(await c.list({"atlas.user": user})) for c in self.servers.values()])
        if count >= QUOTA_PER_USER:
            raise QuotaExceeded(
                f"该用户已有 {count} 个沙箱（上限 {QUOTA_PER_USER}），先关掉一些旧会话再试"
            )


# ─────────────────────────────────────────────────────────────── 回收


class Maintainer:
    def __init__(self, ensurer: Ensurer) -> None:
        self._e = ensurer

    async def reap_idle(self) -> None:
        """原生沙箱：没有运行中的命令、且闲置超过 IDLE_TTL_S → 先删路由，再删沙箱。"""
        routes = self._e.routes
        for ws in routes.workspaces():
            if running := routes.running(ws):
                say("回收", f"工作区 {ws} 有 {len(running)} 条命令在跑 → 不回收（不设运行上限）")
                continue
            idle = routes.idle_for(ws)
            if idle < IDLE_TTL_S:
                continue
            async with self._e._r.lock(f"ensure:{SBX}:{ws}"):  # 与 ensure 互斥
                ep = routes.get(ws)
                if ep is None:
                    continue
                self._e.forget(SBX, ws)  # ★ 先删路由：之后的文件操作走对象存储
                await self._e.servers[SBX].delete(ep.sandbox_id)  # 写穿：没有待上传的数据，不用排空
                say("回收", f"工作区 {ws} 闲置 {idle:.1f} 秒 → 删路由、删沙箱 {ep.sandbox_id}")

    async def reap_orphans(self, live_threads: set[str]) -> None:
        """会话已经不在的沙箱（server 崩溃留下的、并发 ensure 多建的）一律删掉。"""
        for kind, client in self._e.servers.items():
            for sb in await client.list({"atlas.managed": "true"}):
                thread = sb["metadata"]["atlas.thread"]
                current = self._e._r.get(f"ep:{kind}:{thread}")
                if thread not in live_threads or (current and current.sandbox_id != sb["id"]):
                    say("回收", f"孤儿 {sb['id']}（会话 {thread}）→ 删除")
                    await client.delete(sb["id"])

    async def release(self, thread: str) -> None:
        """会话删除：两种沙箱、路由、端点缓存都清掉。"""
        for kind, client in self._e.servers.items():
            for sb in await client.list({"atlas.thread": thread}):
                await client.delete(sb["id"])
            self._e.forget(kind, thread)
