"""抽取队列（记忆设计 §11）。

    Run 完成 ──LPUSH──▶ atlas:memory:queue ──BLMOVE──▶ processing:{实例} ──▶ worker ──▶ Mem0
                              │                                  └── 处理完 LREM
                              └── 失败重试；超上限 ──▶ atlas:memory:dead

★ 可靠消费（横向扩展设计 §7.2）：取出与「放进本实例的处理中列表」是原子的
  （BLMOVE），处理完才删。抽取中途进程退出，任务还在处理中列表里，维护者把
  「所属实例已不在」的列表搬回主队列。重复投递无害：同一个 run 只抽一次。

★ 记忆**绝不能成为会话的硬依赖**。入队是 fire-and-forget：Redis 抽风、
  Mem0 挂了、Qdrant 没起来，用户都照常能发消息。代价是要把失败喊出来，
  否则就成了「以为在记其实没记」。

★ 为什么用 Redis list 而不是 asyncio.create_task：§11 要求失败能重试、
  重试耗尽落死信。进程内 task 一重启就全丢了，而抽取失败最常见的原因
  恰恰是下游暂时不可用 —— 那正是值得重试的情形。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from .converge import converge
from .extractor import ExtractionJob

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from .client import MemoryClient

logger = logging.getLogger(__name__)

__all__ = [
    "DEAD_KEY",
    "QUEUE_KEY",
    "enqueue",
    "recover_orphaned",
    "revive_dead",
    "run_worker",
]

QUEUE_KEY = "atlas:memory:queue"
DEAD_KEY = "atlas:memory:dead"
PROCESSING_PREFIX = "atlas:memory:processing:"
#: 抽过的 run（去重）。保留一周：重复投递只会发生在分钟级的窗口里
_DONE_PREFIX = "atlas:memory:done:"
_DONE_TTL_S = 7 * 86_400
#: 死信最多被维护者复活几轮。还不行就留在死信里等人看 —— 无限复活等于无限烧 LLM
_MAX_REVIVALS = 3


def processing_key(instance: str) -> str:
    return f"{PROCESSING_PREFIX}{instance}"


#: BRPOP 的单次阻塞时长。短一些，好让 worker 能及时响应取消。
_POLL_S = 5


async def enqueue(redis: aioredis.Redis, job: ExtractionJob) -> None:
    """入队。★ 永不抛 —— 它跑在 run 的收尾路径上。"""
    try:
        await redis.lpush(QUEUE_KEY, json.dumps(job.to_json(), ensure_ascii=False))
    except Exception:
        logger.warning("记忆抽取入队失败，本轮不会被记住", exc_info=True)


async def run_worker(
    redis: aioredis.Redis, memory: MemoryClient, *, max_attempts: int, instance: str
) -> None:
    """常驻 worker。由 main.py 的 lifespan 起停。

    ★ 单条任务失败不影响后续 —— 每轮都自己兜异常。一次抽取炸掉就让整个
      worker 退出的话，表现是「从某个时刻起再也不记东西了」，而且没有
      任何错误浮到用户面前。
    """
    logger.info("记忆抽取 worker 已启动")
    processing = processing_key(instance)
    while True:
        try:
            # LPUSH 入队、右端取 —— 与原来的 BRPOP 同一个顺序
            raw = await redis.blmove(QUEUE_KEY, processing, _POLL_S, "RIGHT", "LEFT")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("记忆队列读取出错，稍后重试", exc_info=True)
            await asyncio.sleep(2)
            continue

        if raw is None:
            continue

        try:
            job = ExtractionJob.from_json(json.loads(raw))
        except Exception:
            logger.warning("记忆任务无法解析，丢弃：%r", raw[:200], exc_info=True)
            job = None

        if job is not None:
            await _handle(redis, memory, job, max_attempts=max_attempts)
        # ★ 处理完（含失败后已重新入队 / 落死信）才从处理中列表删掉
        with contextlib.suppress(Exception):
            await redis.lrem(processing, 1, raw)


async def recover_orphaned(redis: aioredis.Redis, live_instances: set[str]) -> int:
    """把「所属实例已不在」的处理中列表搬回主队列。返回搬了几条。维护者定期调用。"""
    moved = 0
    async for key in redis.scan_iter(match=f"{PROCESSING_PREFIX}*"):
        name = key.decode() if isinstance(key, bytes) else str(key)
        if name.removeprefix(PROCESSING_PREFIX) in live_instances:
            continue
        while await redis.lmove(name, QUEUE_KEY, "RIGHT", "RIGHT") is not None:
            moved += 1
    if moved:
        logger.warning("记忆队列：%d 条任务的处理实例已退出，已放回队列", moved)
    return moved


async def revive_dead(redis: aioredis.Redis) -> int:
    """死信按轮次重新入队（下游恢复后它们就能成功）。返回复活了几条。维护者定期调用。"""
    revived = 0
    for raw in await redis.lrange(DEAD_KEY, 0, -1):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        rounds = int(data.get("revived") or 0)
        if rounds >= _MAX_REVIVALS:
            continue
        await redis.lpush(
            QUEUE_KEY,
            json.dumps({**data, "attempt": 0, "revived": rounds + 1}, ensure_ascii=False),
        )
        await redis.lrem(DEAD_KEY, 1, raw)
        revived += 1
    if revived:
        logger.info("记忆死信：%d 条重新入队", revived)
    return revived


async def _handle(
    redis: aioredis.Redis,
    memory: MemoryClient,
    job: ExtractionJob,
    *,
    max_attempts: int,
) -> None:
    from uuid import UUID

    run = str(job.metadata.get("source_run") or "")
    if run and await redis.exists(_DONE_PREFIX + run):
        return  # 重复投递（处理中列表被搬回过）：这个 run 已经抽过
    try:
        user_id = UUID(job.user_id)
        result = await memory.add(job.messages, user_id=user_id, metadata=job.metadata)
        added = _added(result)
        # ★ 收敛：mem0 2.1 的抽取结构性地只增不改（"Your sole operation is
        #   ADD"），事实变更会与旧事实并存。这一层把被取代的旧记忆清掉。
        #   见 converge.py 的论证。只在产生了新事实时才跑。
        removed = await converge(memory, user_id=user_id, added=added) if added else 0
        if run:
            with contextlib.suppress(Exception):
                await redis.set(_DONE_PREFIX + run, "1", ex=_DONE_TTL_S)
        logger.info(
            "记忆抽取完成 run=%s 新增=%d 收敛删除=%d",
            job.metadata.get("source_run"),
            len(added),
            removed,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        attempt = job.attempt + 1
        if attempt >= max_attempts:
            logger.exception(
                "记忆抽取重试 %d 次仍失败，落死信 run=%s",
                attempt,
                job.metadata.get("source_run"),
            )
            with contextlib.suppress(Exception):
                await redis.lpush(
                    DEAD_KEY,
                    json.dumps(
                        {**job.to_json(), "attempt": attempt}, ensure_ascii=False
                    ),
                )
            return
        logger.warning(
            "记忆抽取失败，第 %d 次重试 run=%s",
            attempt,
            job.metadata.get("source_run"),
            exc_info=True,
        )
        # ★ 重新入队而不是原地重试：原地重试会把 worker 堵在一个坏任务上，
        #   后面排队的全部饿死。
        with contextlib.suppress(Exception):
            await redis.lpush(
                QUEUE_KEY,
                json.dumps({**job.to_json(), "attempt": attempt}, ensure_ascii=False),
            )


def _added(result: object) -> list[dict]:
    """从 add 的返回里取出**新增**的那些记忆。

    ★ mem0 的返回里每条带 event（ADD / NONE 等）。只有 ADD 的才是新事实，
      拿全部去做收敛会让「什么都没变」的轮次也去删东西。
    """
    rows = result.get("results") if isinstance(result, dict) else result
    out = []
    for r in rows or []:
        if isinstance(r, dict) and str(r.get("event", "ADD")).upper() == "ADD":
            out.append(r)
    return out
