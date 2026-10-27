"""沙箱的供给与回收 —— 原来 cluster 的那部分判断。

doc/sandbox-opensandbox-design.html §13、§14。

    SandboxRoutes        Redis 路由表 atlas:{kind}:route:{key}（= 端点缓存）。原生沙箱：有记录 =
                         工作区有运行中的沙箱，文件工具走沙箱（§9）；另记运行中的命令与最近活动时间
    Provisioner          两种沙箱共用的判断：会话级锁内按 metadata 复用 → 配额 → 创建 → 存令牌
                         → 解析端点 → 等就绪 → 写路由表；孤儿回收、会话删除
    SandboxProvisioner   原生沙箱（opensandbox-sbx）：镜像校验、execd 就绪、闲置回收
                         ACP Pod 的那一份在 acp.py（AcpProvisioner）

★ 创建请求不能由客户端指定 id，并发的 ensure 会建出两个沙箱：
  所以「查了再建」必须在锁里（RedisLock）。
★ 令牌由 Atlas 生成、经 env 交给沙箱，而 GET /sandboxes 不会把 env 还回来 —— 令牌只能 Atlas 自己存
  （atlas:{kind}:token:{sandbox_id}）。存丢了就连不上，只能删掉重建（demo 实验 8）。
★ Pending 不能当坏的删：可能是另一个 ensure 正在建，或建到一半崩掉的进程留下的。先等它离开 Pending。
★ 一个会话有多个可用沙箱时（锁失效才会出现）：留上次用的那个，其余当场删 —— 两个挂载就是两个客户端，
  断线重连还可能接到另一个沙箱上（demo 实验 2）。
★ server 报 Running 时 Pod 里的进程未必在监听（P0 实测：带 egress sidecar 时 execd 晚约 1 秒）：
  新建或复用之后先等就绪，再把端点交出去。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from atlas_redislock import LockBusy, LockLost, RedisLock

from .execd import TOKEN_HEADER, ExecdClient, ExecdGone
from .lifecycle import LifecycleClient, LifecycleError

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ...config import Settings

__all__ = [
    "EXECD_PORT",
    "ImageDenied",
    "Provisioner",
    "QuotaExceeded",
    "SandboxEndpoint",
    "SandboxProvisioner",
    "SandboxRoutes",
    "lifecycle_clients",
    "sandbox_spec",
]

logger = logging.getLogger(__name__)

EXECD_PORT = 44772

#: 可以用的状态。★ Allocated = Pod 有了 IP、还没报 ready：上游在创建时把它当作「可以接请求」
#: （_normalize_create_status），但 GET / 列表里会原样返回
_USABLE = frozenset({"Running", "Allocated"})
#: 过渡态：等它落定，不当坏的删
_SETTLING = frozenset({"Pending", "Resuming"})
#: 已经在删 / 删完了：不占配额
_LEAVING = frozenset({"Stopping", "Terminated"})

#: 进程内的路由缓存：{kind}:{key} → (过期时刻, 端点或 None)。别的进程的改动最多晚这么久被看到
_CACHE: dict[str, tuple[float, SandboxEndpoint | None]] = {}


class QuotaExceeded(RuntimeError):
    pass


class ImageDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxEndpoint:
    sandbox_id: str
    url: str
    token: str


# ─────────────────────────────────────────────────────────────── 路由表


class SandboxRoutes:
    def __init__(self, redis: aioredis.Redis, *, cache_s: float = 2.0, kind: str = "sbx") -> None:
        self._r = redis
        self._cache_s = cache_s
        self._kind = kind
        self._route = f"atlas:{kind}:route:"
        self._running = f"atlas:{kind}:running:"
        self._active = f"atlas:{kind}:active:"

    async def get(self, ws: str, *, fresh: bool = False) -> SandboxEndpoint | None:
        now = time.monotonic()
        hit = _CACHE.get(f"{self._kind}:{ws}")
        if not fresh and hit and hit[0] > now:
            return hit[1]
        raw = await self._r.get(self._route + ws)
        ep = SandboxEndpoint(**json.loads(raw)) if raw else None
        _CACHE[f"{self._kind}:{ws}"] = (now + self._cache_s, ep)
        return ep

    async def set(self, ws: str, ep: SandboxEndpoint) -> None:
        await self._r.set(self._route + ws, json.dumps(asdict(ep)))
        _CACHE[f"{self._kind}:{ws}"] = (time.monotonic() + self._cache_s, ep)
        await self.touch(ws)

    async def delete(self, ws: str) -> None:
        await self._r.delete(self._route + ws)
        _CACHE.pop(f"{self._kind}:{ws}", None)

    def forget_cached(self, ws: str) -> None:
        """操作失败时作废进程内缓存，下次从 Redis 重读。"""
        _CACHE.pop(f"{self._kind}:{ws}", None)

    async def workspaces(self) -> list[str]:
        return [
            k.removeprefix(self._route) async for k in self._r.scan_iter(match=f"{self._route}*")
        ]

    # ------------------------------------------------------------------ 活动记录

    async def started(self, ws: str, sandbox_id: str, cmd_id: str) -> None:
        await self._r.sadd(self._running + ws, f"{sandbox_id}|{cmd_id}")
        await self.touch(ws)

    async def finished(self, ws: str, sandbox_id: str, cmd_id: str) -> None:
        await self._r.srem(self._running + ws, f"{sandbox_id}|{cmd_id}")
        await self.touch(ws)

    async def running(self, ws: str) -> set[str]:
        return set(await self._r.smembers(self._running + ws))

    async def drop_running(self, ws: str, member: str) -> None:
        """清掉一条残留的运行记录。★ 不算活动：否则回收一次就把闲置计时清零了。"""
        await self._r.srem(self._running + ws, member)

    async def touch(self, ws: str) -> None:
        await self._r.set(self._active + ws, str(time.time()))

    async def idle_for(self, ws: str) -> float:
        raw = await self._r.get(self._active + ws)
        return time.time() - float(raw) if raw else float("inf")

    async def clear_activity(self, ws: str) -> None:
        await self._r.delete(self._running + ws, self._active + ws)


# ─────────────────────────────────────────────────────────────── 共用的供给逻辑


def lifecycle_clients(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, LifecycleClient]:
    """两个 lifecycle server 实例（§7）：kind → 客户端。"""
    pairs = {
        "sbx": (settings.opensandbox_sbx_url, settings.opensandbox_sbx_api_key),
        "acp": (settings.opensandbox_acp_url, settings.opensandbox_acp_api_key),
    }
    return {
        kind: LifecycleClient(
            str(url),
            key.get_secret_value(),
            timeout_s=settings.opensandbox_timeout_s,
            transport=transport,
        )
        for kind, (url, key) in pairs.items()
    }


class Provisioner:
    """一种沙箱（kind）的供给与回收。子类给出 spec、准入与就绪的具体做法。"""

    kind: ClassVar[str]
    #: 端点解析用的容器端口（原生沙箱 = execd，ACP = bridge）
    port: ClassVar[int]

    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        lifecycle: LifecycleClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._r = redis
        self._transport = transport
        self._clients = lifecycle_clients(settings, transport=transport)
        self.lifecycle = lifecycle or self._clients[self.kind]
        self.routes = SandboxRoutes(redis, cache_s=settings.sandbox_route_cache_s, kind=self.kind)
        self._token_key = f"atlas:{self.kind}:token:"
        self._lock_key = f"atlas:{self.kind}:ensure:"

    # ------------------------------------------------------------------ 子类钩子

    async def _ready(self, ep: SandboxEndpoint) -> None:
        """等沙箱里的服务真的在监听。默认不等。"""

    # ------------------------------------------------------------------ 供给

    async def _provision(
        self,
        key: str,
        *,
        user: str,
        previous: SandboxEndpoint | None,
        admit: Callable[[], None],
        build: Callable[[str], dict[str, Any]],
        after_create: Callable[[str], Awaitable[None]] | None = None,
    ) -> SandboxEndpoint:
        """锁内调用：复用可用的，没有就按 build(token) 建一个。"""
        sandbox_id = await self._reusable(key, previous.sandbox_id if previous else None)
        if sandbox_id is None:
            admit()
            await self._admit_quota(user)
            token = secrets.token_urlsafe(24)
            created = await self.lifecycle.create(build(token))
            sandbox_id = str(created["id"])
            await self._r.set(self._token_key + sandbox_id, token)
            state = (created.get("status") or {}).get("state")
            if state not in _USABLE:
                await self._discard(sandbox_id)
                msg = (created.get("status") or {}).get("message") or state
                raise LifecycleError(f"沙箱 {sandbox_id} 没有起来：{msg}")
            logger.info("为 %s %s 建了沙箱 %s", self.kind, key, sandbox_id)
            if after_create is not None:
                try:
                    await after_create(sandbox_id)
                except BaseException:
                    await self._discard(sandbox_id)  # 半成品不留：下一次 ensure 重新建
                    raise
        token = await self._r.get(self._token_key + sandbox_id)
        url = await self.lifecycle.endpoint(sandbox_id, self.port)
        ep = SandboxEndpoint(sandbox_id, url, str(token))
        await self._ready(ep)
        await self.routes.set(key, ep)
        return ep

    async def _locked(
        self, key: str, fn: Callable[[], Awaitable[SandboxEndpoint]]
    ) -> SandboxEndpoint:
        lock = RedisLock(self._r, self._lock_key + key, ttl_s=30)
        try:
            async with lock.hold(wait_s=self._settings.opensandbox_timeout_s):
                return await fn()
        except LockBusy as exc:
            raise LifecycleError(f"{key} 的沙箱正在由别处创建，等待超时") from exc
        except LockLost as exc:
            raise LifecycleError(f"{key} 的沙箱创建锁中途丢失，请重试") from exc

    async def _reusable(self, key: str, prefer: str | None) -> str | None:
        usable: list[dict[str, Any]] = []
        for sb in await self.lifecycle.list({"atlas.kind": self.kind, "atlas.thread": key}):
            state = _state(sb)
            if state in _SETTLING:
                state = await self._wait_settled(sb["id"])
            if state in _USABLE and await self._r.get(self._token_key + sb["id"]):
                usable.append(sb)
                continue
            reason = "令牌不在" if state in _USABLE else state
            logger.info("沙箱 %s 用不了（%s）→ 删掉", sb["id"], reason)
            await self._discard(sb["id"])
        if not usable:
            return None
        usable.sort(key=lambda sb: (sb["id"] != prefer, _neg_time(sb)))  # 上次用的 > 最新的
        for extra in usable[1:]:
            logger.warning("%s 有多个沙箱：留 %s，删掉 %s", key, usable[0]["id"], extra["id"])
            await self._discard(extra["id"])
        return str(usable[0]["id"])

    async def _wait_settled(self, sandbox_id: str) -> str:
        deadline = time.monotonic() + self._settings.opensandbox_timeout_s
        while time.monotonic() < deadline:
            sb = await self.lifecycle.get(sandbox_id)
            state = _state(sb) if sb else "Deleted"
            if state not in _SETTLING:
                return state
            await asyncio.sleep(1.0)
        return "Pending"

    async def _admit_quota(self, user: str) -> None:
        """★ 原生沙箱与 ACP Pod 合计（§13），两个 server 各数一遍。先数再建：软上限。"""
        count = 0
        seen: set[str] = set()
        for client in self._clients.values():
            if client.base_url in seen:  # 两个实例指向同一个 server（测试、单实例部署）只数一次
                continue
            seen.add(client.base_url)
            boxes = await client.list({"atlas.managed": "true", "atlas.user": user})
            count += sum(1 for sb in boxes if _state(sb) not in _LEAVING)
        limit = self._settings.sandbox_quota_per_user
        if count >= limit:
            raise QuotaExceeded(
                f"该用户已有 {count} 个沙箱 / 会话 Pod（上限 {limit}），先关掉一些旧会话再试"
            )

    async def _discard(self, sandbox_id: str) -> None:
        await self.lifecycle.delete(sandbox_id)
        await self._r.delete(self._token_key + sandbox_id)

    # ------------------------------------------------------------------ 回收

    async def is_gone(self, ep: SandboxEndpoint) -> bool:
        """沙箱是不是真的不在了（请求失败时问一句，避免把网络抖动当成沙箱没了）。"""
        try:
            sb = await self.lifecycle.get(ep.sandbox_id)
        except LifecycleError:
            return False  # 问不到就当它还在：宁可报错，也不悄悄换一个写入方
        return sb is None or _state(sb) not in _USABLE

    async def forget(self, key: str, ep: SandboxEndpoint | None = None) -> None:
        """删路由（只删仍指向这个沙箱的那条）。"""
        current = await self.routes.get(key, fresh=True)
        if ep is None or (current and current.sandbox_id == ep.sandbox_id):
            await self.routes.delete(key)

    async def reap_orphans(self, live_threads: set[str]) -> None:
        """会话已经不在的沙箱一律删掉（建好不到 sandbox_reap_min_age_s 的除外）。"""
        now = datetime.now(UTC)
        for sb in await self.lifecycle.list({"atlas.managed": "true", "atlas.kind": self.kind}):
            key = (sb.get("metadata") or {}).get("atlas.thread", "")
            if key in live_threads:
                continue
            created = _parse_time(sb.get("createdAt"))
            if created and (now - created).total_seconds() < self._settings.sandbox_reap_min_age_s:
                continue
            logger.info("回收孤儿沙箱 %s（%s %s 已不存在）", sb["id"], self.kind, key)
            await self._discard(sb["id"])
            await self.forget(key, SandboxEndpoint(sb["id"], "", ""))

    async def release(self, key: str) -> None:
        """会话删除：它的沙箱、路由、活动记录都清掉。失败只记日志，孤儿回收兜底。"""
        try:
            await self.routes.delete(key)
            await self.routes.clear_activity(key)
            for sb in await self.lifecycle.list({"atlas.kind": self.kind, "atlas.thread": key}):
                await self._discard(sb["id"])
        except Exception:
            logger.warning("%s 沙箱级联清理失败 %s", self.kind, key, exc_info=True)


# ─────────────────────────────────────────────────────────────── 原生沙箱


def sandbox_spec(
    settings: Settings, *, ws: str, user: str, token: str, image: str
) -> dict[str, Any]:
    """原生沙箱的创建请求（设计 §8.1）。前缀布局与 OssFilesystem 一致：{user}/{ws}/workspace。"""
    return {
        "image": {"uri": image},
        "entrypoint": ["tail", "-f", "/dev/null"],
        "timeout": None,  # 不自动过期：回收由 Atlas 做（不设运行上限）
        "resourceLimits": {
            "cpu": settings.sandbox_cpu_limit,
            "memory": settings.sandbox_memory_limit,
        },
        "resourceRequests": {
            "cpu": settings.sandbox_cpu_request,
            "memory": settings.sandbox_memory_request,
        },
        "env": {
            "EXECD_ACCESS_TOKEN": token,
            "EXECD_INIT": "1",  # execd 作 PID 1：回收僵尸进程、转发信号
            "EXECD_ENVS": "/tmp/.execd/env",  # 默认在 /opt/opensandbox，而 strict 档的根是只读的
            "HOME": "/home/agent",
            "TMPDIR": "/tmp",
        },
        "metadata": {
            "atlas.managed": "true",
            "atlas.kind": "sbx",
            "atlas.thread": ws,
            "atlas.user": user,
        },
        "volumes": [
            pvc_volume(settings, "workspace", f"{user}/{ws}/workspace", "/workspace"),
            pvc_volume(settings, "skills", f"{user}/{ws}/skills", "/skills", read_only=True),
        ],
    }


def pvc_volume(
    settings: Settings, name: str, sub_path: str, mount: str, *, read_only: bool = False
) -> dict[str, Any]:
    """整桶 PVC 上的一个前缀（§6.1）。"""
    return {
        "name": name,
        "pvc": {"claimName": settings.sandbox_pvc, "createIfNotExists": False},
        "mountPath": mount,
        "subPath": sub_path,
        "readOnly": read_only,
    }


class SandboxProvisioner(Provisioner):
    kind = "sbx"
    port = EXECD_PORT

    async def ensure(
        self, ws: str, *, user: str, image: str = "", fresh: bool = False
    ) -> SandboxEndpoint:
        """确保工作区有一个运行中的沙箱。fresh = 不信缓存（连不上它的时候）。"""
        if not fresh and (ep := await self.routes.get(ws)):
            return ep
        image = image or self._settings.sandbox_default_image

        async def locked() -> SandboxEndpoint:
            previous = await self.routes.get(ws, fresh=True)
            if previous and not fresh:
                return previous  # 刚被并发的另一个 ensure 建好
            return await self._provision(
                ws,
                user=user,
                previous=previous,
                admit=lambda: self._admit_image(image),
                build=lambda token: sandbox_spec(
                    self._settings, ws=ws, user=user, token=token, image=image
                ),
            )

        return await self._locked(ws, locked)

    def _admit_image(self, image: str) -> None:
        settings = self._settings
        if image != settings.sandbox_default_image and not any(
            image.startswith(p) for p in settings.sandbox_image_allow_prefixes
        ):
            raise ImageDenied(f"沙箱镜像 {image!r} 不在允许范围内")

    async def _ready(self, ep: SandboxEndpoint) -> None:
        """execd 的 /ping 通了才算就绪。"""
        deadline = time.monotonic() + self._settings.opensandbox_timeout_s
        async with httpx.AsyncClient(
            timeout=5.0,
            transport=self._transport,
            trust_env=False,
            headers={TOKEN_HEADER: ep.token},
        ) as client:
            while True:
                try:
                    if (await client.get(f"{ep.url}/ping")).status_code < 500:
                        return
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise LifecycleError(f"沙箱 {ep.sandbox_id} 的 execd 没有就绪")
                await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ 闲置回收

    async def reap_idle(self) -> None:
        """没有运行中的命令、且闲置超过 sandbox_idle_ttl_s → 先删路由，再删沙箱。

        写穿式挂载下文件早已在对象存储里：删沙箱前不用排空。
        """
        for ws in await self.routes.workspaces():
            ep = await self.routes.get(ws, fresh=True)
            if ep is None or await self._still_running(ws, ep):
                continue
            if await self.routes.idle_for(ws) < self._settings.sandbox_idle_ttl_s:
                continue
            lock = RedisLock(self._r, self._lock_key + ws, ttl_s=30)
            try:
                async with lock.hold():  # 与 ensure 互斥：不在别人正复用它的时候删
                    current = await self.routes.get(ws, fresh=True)
                    if current is None or current.sandbox_id != ep.sandbox_id:
                        continue
                    await self.routes.delete(ws)  # ★ 先删路由：之后的文件操作走对象存储
                    await self._discard(ep.sandbox_id)
                    await self.routes.clear_activity(ws)
                    logger.info("工作区 %s 的沙箱 %s 闲置，已回收", ws, ep.sandbox_id)
            except LockBusy:
                continue

    async def _still_running(self, ws: str, ep: SandboxEndpoint) -> bool:
        """运行中的命令集合可能残留（server 崩在命令中途）：逐个问 execd 确认。"""
        members = await self.routes.running(ws)
        if not members:
            return False
        execd = ExecdClient(ep.url, ep.token, transport=self._transport)
        alive = False
        for member in members:
            sandbox_id, _, cmd_id = member.partition("|")
            try:
                running = sandbox_id == ep.sandbox_id and (await execd.status(cmd_id)).get(
                    "running"
                )
            except ExecdGone:
                running = False
            except Exception:
                return True  # 问不到：当它还在跑（不设运行上限 —— 宁可晚回收）
            if running:
                alive = True
            else:
                await self.routes.drop_running(ws, member)
        return alive


def _state(sb: dict[str, Any]) -> str:
    return str((sb.get("status") or {}).get("state", ""))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _neg_time(sb: dict[str, Any]) -> float:
    created = _parse_time(sb.get("createdAt"))
    return -created.timestamp() if created else 0.0
