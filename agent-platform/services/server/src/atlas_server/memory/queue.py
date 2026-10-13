"""抽取队列（记忆设计 §11）。

    Run 完成 ──LPUSH──▶ atlas:memory:queue ──BRPOP──▶ worker ──▶ Mem0
                              │
                              └── 失败重试；超上限 ──▶ atlas:memory:dead

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

__all__ = ["DEAD_KEY", "QUEUE_KEY", "enqueue", "run_worker"]

QUEUE_KEY = "atlas:memory:queue"
DEAD_KEY = "atlas:memory:dead"

#: BRPOP 的单次阻塞时长。短一些，好让 worker 能及时响应取消。
_POLL_S = 5


async def enqueue(redis: aioredis.Redis, job: ExtractionJob) -> None:
    """入队。★ 永不抛 —— 它跑在 run 的收尾路径上。"""
    try:
        await redis.lpush(QUEUE_KEY, json.dumps(job.to_json(), ensure_ascii=False))
    except Exception:
        logger.warning("记忆抽取入队失败，本轮不会被记住", exc_info=True)


async def run_worker(
    redis: aioredis.Redis, memory: MemoryClient, *, max_attempts: int
) -> None:
    """常驻 worker。由 main.py 的 lifespan 起停。

    ★ 单条任务失败不影响后续 —— 每轮都自己兜异常。一次抽取炸掉就让整个
      worker 退出的话，表现是「从某个时刻起再也不记东西了」，而且没有
      任何错误浮到用户面前。
    """
    logger.info("记忆抽取 worker 已启动")
    while True:
        try:
            item = await redis.brpop([QUEUE_KEY], timeout=_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("记忆队列读取出错，稍后重试", exc_info=True)
            await asyncio.sleep(2)
            continue

        if item is None:
            continue

        try:
            job = ExtractionJob.from_json(json.loads(item[1]))
        except Exception:
            logger.warning("记忆任务无法解析，丢弃：%r", item[1][:200], exc_info=True)
            continue

        await _handle(redis, memory, job, max_attempts=max_attempts)


async def _handle(
    redis: aioredis.Redis,
    memory: MemoryClient,
    job: ExtractionJob,
    *,
    max_attempts: int,
) -> None:
    from uuid import UUID

    try:
        user_id = UUID(job.user_id)
        result = await memory.add(job.messages, user_id=user_id, metadata=job.metadata)
        added = _added(result)
        # ★ 收敛：mem0 2.1 的抽取结构性地只增不改（"Your sole operation is
        #   ADD"），事实变更会与旧事实并存。这一层把被取代的旧记忆清掉。
        #   见 converge.py 的论证。只在产生了新事实时才跑。
        removed = await converge(memory, user_id=user_id, added=added) if added else 0
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
