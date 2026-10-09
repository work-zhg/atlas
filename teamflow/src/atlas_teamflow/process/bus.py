"""实例间协调：流程事件广播、Agent 唤醒、节点租约锁、团队级别缓存失效。

两种实现，接口相同：
- LocalCoordinator：进程内（单实例 / 测试）；
- RedisCoordinator：经 Redis（多实例）。广播用发布 / 订阅，节点锁用 SET NX PX + 续租。

广播的消息：
- nudge：有新的流程事件落库了（事务**提交之后**才发），订阅方按游标去库里取 —— 库是唯一真相；
- delta：Agent 输出的增量（不落库），只为逐段显示。

★ 节点租约不是工作时长上限：持有者每 lease/3 续租一次，Agent 跑多久都可以；
  只有持有者进程死掉（不再续租）时租约才过期，由其他实例接手（atlas-no-timeouts 原则）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

__all__ = [
    "LocalCoordinator",
    "RedisCoordinator",
    "coordinator",
    "make_coordinator",
    "nudge_after_commit",
    "set_coordinator",
    "wake_after_commit",
]

log = logging.getLogger("atlas_teamflow.coord")


class LocalCoordinator:
    def __init__(self) -> None:
        self._subs: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._wake_hooks: list[Callable[[UUID], None]] = []
        self._level_hooks: list[Callable[[UUID], None]] = []
        self._locks: dict[UUID, str] = {}
        self._bg: set[asyncio.Task[Any]] = set()

    # ── 生命周期
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ── 订阅（本实例的 SSE 连接）
    def subscribe(self, process_uuid: UUID) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs[process_uuid].add(q)
        return q

    def unsubscribe(self, process_uuid: UUID, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs[process_uuid].discard(q)
        if not self._subs[process_uuid]:
            self._subs.pop(process_uuid, None)

    def deliver(self, process_uuid: UUID, msg: dict[str, Any]) -> None:
        """投给本实例的订阅者。慢消费者丢增量；nudge 丢了也无妨，心跳会按游标补齐。"""
        for q in list(self._subs.get(process_uuid, ())):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    # ── 广播（全部实例）
    def publish(self, process_uuid: UUID, msg: dict[str, Any]) -> None:
        self.deliver(process_uuid, msg)

    def nudge(self, process_uuid: UUID) -> None:
        self.publish(process_uuid, {"kind": "nudge"})

    # ── Agent 唤醒（全部实例；拿到节点锁的那个去干活）
    def on_wake(self, fn: Callable[[UUID], None]) -> None:
        self._wake_hooks.append(fn)

    def wake(self, node_uuid: UUID) -> None:
        for fn in self._wake_hooks:
            fn(node_uuid)

    # ── 团队级别缓存失效（全部实例）
    def on_team_changed(self, fn: Callable[[UUID], None]) -> None:
        self._level_hooks.append(fn)

    def team_changed(self, team_uuid: UUID) -> None:
        for fn in self._level_hooks:
            fn(team_uuid)

    # ── 节点租约锁
    async def acquire(self, node_uuid: UUID, token: str) -> bool:
        if node_uuid in self._locks:
            return False
        self._locks[node_uuid] = token
        return True

    async def renew(self, node_uuid: UUID, token: str) -> bool:
        return self._locks.get(node_uuid) == token

    async def release(self, node_uuid: UUID, token: str) -> None:
        if self._locks.get(node_uuid) == token:
            self._locks.pop(node_uuid, None)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisCoordinator(LocalCoordinator):
    """多实例：所有广播都经 Redis 再回到各实例（包括自己），本地不直接投递。"""

    def __init__(self, url: str, lease_seconds: int = 30, prefix: str = "tf:") -> None:
        super().__init__()
        # 频道是 Redis 全局的（不分 db），前缀隔离不同部署 / 测试
        self.PROC, self.WAKE = f"{prefix}proc:", f"{prefix}wake"
        self.TEAM, self.LOCK = f"{prefix}team", f"{prefix}node-lock:"
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(url, decode_responses=True)
        self.lease_ms = lease_seconds * 1000
        self.instance = uuid4().hex[:8]
        self._listener: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._listener is None:
            self._listener = asyncio.get_running_loop().create_task(self._listen())
            await self._ready.wait()

    async def close(self) -> None:
        if self._listener:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listener
        await self.redis.aclose()

    async def _listen(self) -> None:
        """一个实例一条订阅连接；断开后重连（不判失败）。"""
        while True:
            try:
                pubsub = self.redis.pubsub()
                await pubsub.psubscribe(self.PROC + "*")
                await pubsub.subscribe(self.WAKE, self.TEAM)
                self._ready.set()
                async for m in pubsub.listen():
                    kind = m.get("type")
                    if kind == "pmessage":
                        pid = UUID(m["channel"][len(self.PROC) :])
                        self.deliver(pid, json.loads(m["data"]))
                    elif kind == "message" and m["channel"] == self.WAKE:
                        super().wake(UUID(m["data"]))
                    elif kind == "message" and m["channel"] == self.TEAM:
                        super().team_changed(UUID(m["data"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Redis 订阅断开，2s 后重连：%s", exc)
                self._ready.set()
                await asyncio.sleep(2)

    def publish(self, process_uuid: UUID, msg: dict[str, Any]) -> None:
        self._spawn(
            self.redis.publish(self.PROC + str(process_uuid), json.dumps(msg, ensure_ascii=False))
        )

    def wake(self, node_uuid: UUID) -> None:
        self._spawn(self.redis.publish(self.WAKE, str(node_uuid)))

    def team_changed(self, team_uuid: UUID) -> None:
        self._spawn(self.redis.publish(self.TEAM, str(team_uuid)))

    async def acquire(self, node_uuid: UUID, token: str) -> bool:
        return bool(
            await self.redis.set(self.LOCK + str(node_uuid), token, nx=True, px=self.lease_ms)
        )

    async def renew(self, node_uuid: UUID, token: str) -> bool:
        return bool(
            await self.redis.eval(_RENEW, 1, self.LOCK + str(node_uuid), token, self.lease_ms)
        )

    async def release(self, node_uuid: UUID, token: str) -> None:
        await self.redis.eval(_RELEASE, 1, self.LOCK + str(node_uuid), token)


_coord: list[LocalCoordinator] = [LocalCoordinator()]


def coordinator() -> LocalCoordinator:
    return _coord[0]


def set_coordinator(c: LocalCoordinator) -> None:
    _coord[0] = c


def make_coordinator(redis_url: str | None, lease_seconds: int = 30) -> LocalCoordinator:
    return RedisCoordinator(redis_url, lease_seconds) if redis_url else LocalCoordinator()


# ───────────────────────────── 事务提交之后再广播（避免订阅方读到未提交的数据）


def nudge_after_commit(session: Session, process_uuid: UUID) -> None:
    session.info.setdefault("tf_nudge", set()).add(process_uuid)


def wake_after_commit(session: Session, node_uuid: UUID) -> None:
    session.info.setdefault("tf_wake", set()).add(node_uuid)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    c = coordinator()
    for pid in session.info.pop("tf_nudge", set()):
        c.nudge(pid)
    for nid in session.info.pop("tf_wake", set()):
        c.wake(nid)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("tf_nudge", None)
    session.info.pop("tf_wake", None)
