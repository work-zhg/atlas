"""协调层：事务提交后的唤醒广播、流程实时事件扇出到各副本、团队级别缓存的失效广播。

两种实现，接口相同：
    LocalCoordinator   进程内（单实例、测试）
    RedisCoordinator   经 Redis 发布 / 订阅（多实例），一个实例一条订阅连接，断了重连

广播的消息：
    nudge    有新的流程事件落库了（事务提交之后才发），订阅方按游标去库里取 —— 库是唯一事实
    wake     某个节点有指令要投、有 run 要取消，各副本收到后去抢节点会话行（锁到的投递）
    team     某个团队的成员或级别变了，各副本作废这个团队的级别缓存

★ 不再有节点租约：投递靠数据库行锁（SKIP LOCKED），进程死了锁随连接释放。
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

from atlas_tenancy import TenantScope
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

#: 唤醒的回调：(租户, 节点)
WakeHook = Callable[[UUID, UUID], None]


class LocalCoordinator:
    def __init__(self) -> None:
        self._subs: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._wake_hooks: list[WakeHook] = []
        self._team_hooks: list[Callable[[UUID], None]] = []
        self._bg: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # ── 本实例的 SSE 订阅
    def subscribe(self, process_uuid: UUID) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs[process_uuid].add(q)
        return q

    def unsubscribe(self, process_uuid: UUID, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs[process_uuid].discard(q)
        if not self._subs[process_uuid]:
            self._subs.pop(process_uuid, None)

    def deliver(self, process_uuid: UUID, msg: dict[str, Any]) -> None:
        """投给本实例的订阅者。慢消费者丢了也无妨：心跳时按游标补齐。"""
        for q in list(self._subs.get(process_uuid, ())):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    # ── 广播
    def nudge(self, process_uuid: UUID) -> None:
        self.deliver(process_uuid, {"kind": "nudge"})

    def on_wake(self, fn: WakeHook) -> None:
        self._wake_hooks.append(fn)

    def wake(self, tenant_uuid: UUID, node_uuid: UUID) -> None:
        for fn in self._wake_hooks:
            fn(tenant_uuid, node_uuid)

    def on_team_changed(self, fn: Callable[[UUID], None]) -> None:
        self._team_hooks.append(fn)

    def team_changed(self, team_uuid: UUID) -> None:
        for fn in self._team_hooks:
            fn(team_uuid)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


class RedisCoordinator(LocalCoordinator):
    """多实例：所有广播都经 Redis 再回到各实例（包括自己），本地不直接投递。

    ★ 经 atlas_redis.RedisRuntime 管池；订阅用独立长连接，不占短命令池。
    """

    def __init__(self, url: str, prefix: str = "tf:") -> None:
        super().__init__()
        self.PROC, self.WAKE, self.TEAM = f"{prefix}proc:", f"{prefix}wake", f"{prefix}team"
        import redis.asyncio as aioredis
        from atlas_redis import RedisConfig, RedisRuntime

        self._rt = RedisRuntime(RedisConfig(url=url, service="teamflow"))
        self.redis = self._rt.commands
        self._sub_redis = aioredis.from_url(
            url,
            decode_responses=True,
            socket_timeout=None,
            socket_connect_timeout=3.0,
            health_check_interval=30,
        )
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
        with contextlib.suppress(Exception):
            await self._sub_redis.aclose()
        await self._rt.aclose()

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = self._sub_redis.pubsub()
                await pubsub.psubscribe(self.PROC + "*")
                await pubsub.subscribe(self.WAKE, self.TEAM)
                self._ready.set()
                async for m in pubsub.listen():
                    kind = m.get("type")
                    if kind == "pmessage":
                        pid = UUID(m["channel"][len(self.PROC) :])
                        self.deliver(pid, json.loads(m["data"]))
                    elif kind == "message" and m["channel"] == self.WAKE:
                        tenant, node = str(m["data"]).split(":", 1)
                        super().wake(UUID(tenant), UUID(node))
                    elif kind == "message" and m["channel"] == self.TEAM:
                        super().team_changed(UUID(m["data"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Redis 订阅断开，2s 后重连：%s", exc)
                self._ready.set()
                await asyncio.sleep(2)

    def nudge(self, process_uuid: UUID) -> None:
        self._spawn(
            self.redis.with_policy(fail_open=True).publish(
                self.PROC + str(process_uuid),
                json.dumps({"kind": "nudge"}),
                key_pattern="teamflow:proc:*",
            )
        )

    def wake(self, tenant_uuid: UUID, node_uuid: UUID) -> None:
        self._spawn(
            self.redis.with_policy(fail_open=True).publish(
                self.WAKE, f"{tenant_uuid}:{node_uuid}", key_pattern="teamflow:wake"
            )
        )

    def team_changed(self, team_uuid: UUID) -> None:
        self._spawn(
            self.redis.with_policy(fail_open=True).publish(
                self.TEAM, str(team_uuid), key_pattern="teamflow:team"
            )
        )


_coord: list[LocalCoordinator] = [LocalCoordinator()]


def coordinator() -> LocalCoordinator:
    return _coord[0]


def set_coordinator(c: LocalCoordinator) -> None:
    _coord[0] = c


def make_coordinator(redis_url: str | None) -> LocalCoordinator:
    return RedisCoordinator(redis_url) if redis_url else LocalCoordinator()


# ───────────────────────────── 事务提交之后再广播（订阅方不会读到未提交的数据）


def nudge_after_commit(session: Session, process_uuid: UUID) -> None:
    session.info.setdefault("tf_nudge", set()).add(process_uuid)


def wake_after_commit(session: Session, node_uuid: UUID) -> None:
    tenant = TenantScope.require()
    session.info.setdefault("tf_wake", set()).add((tenant, node_uuid))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    c = coordinator()
    for pid in session.info.pop("tf_nudge", set()):
        c.nudge(pid)
    for tenant, nid in session.info.pop("tf_wake", set()):
        c.wake(tenant, nid)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("tf_nudge", None)
    session.info.pop("tf_wake", None)
