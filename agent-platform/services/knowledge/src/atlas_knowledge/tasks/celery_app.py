"""Celery 应用（与 Dify 的 worker 同构：Redis 作 broker，按队列分工）。

    队列 dataset   入库、重试（P1）
    队列 enrich    问答对 / 摘要（P2）
    队列 sync      网页 / Git 同步（P2）

★ worker 只用 prefork 池。每个子进程一个事件循环（worker_process_init 里建），任务体用
  run_async(coro) 跑在这个循环上 —— 复用 api 侧的 async 仓储与客户端，不维护两套 DB 访问代码。
★ acks_late + reject_on_worker_lost：worker 子进程被杀时任务回到队列，由下一个 worker 续跑
  （runner 会从已完成的阶段之后继续）。visibility_timeout 设得很长：它到期会导致重复投递，
  不能当超时用。整个 Pod 被杀时消息要等它到期才回来 —— 入库的恢复靠 API 的入库巡检
  （indexing/runner.py recover_stalled），不靠这里。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

from ..settings import get_settings

__all__ = ["celery_app", "get_runtime", "run_async"]

logger = logging.getLogger(__name__)

_settings = get_settings()
celery_app = Celery("atlas_knowledge", broker=_settings.redis_url)
celery_app.conf.update(
    task_default_queue="dataset",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_transport_options={"visibility_timeout": 7 * 24 * 3600},
    broker_connection_retry_on_startup=True,
    imports=("atlas_knowledge.tasks.indexing",),
)

_loop: asyncio.AbstractEventLoop | None = None
_runtime: Any = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return _ensure_loop().run_until_complete(coro)


def get_runtime() -> Any:
    """子进程级 Runtime（模型、Qdrant、Redis 连接都绑定在本进程的事件循环上）。"""
    global _runtime
    if _runtime is None:
        from ..runtime import build_runtime

        _runtime = build_runtime(get_settings())
    return _runtime


@worker_process_init.connect
def _init_process(**_: Any) -> None:
    from ..telemetry import setup_telemetry

    _ensure_loop()
    # ★ 每个子进程各装一份：BatchSpanProcessor 的导出线程不能跨 fork 继承
    setup_telemetry(get_settings(), service_name=f"{get_settings().otel_service_name}-worker")


@worker_process_shutdown.connect
def _shutdown_process(**_: Any) -> None:
    from ..telemetry import shutdown_telemetry

    global _runtime
    shutdown_telemetry()
    if _runtime is not None and _loop is not None and not _loop.is_closed():
        try:
            _loop.run_until_complete(_runtime.aclose())
        except Exception:
            logger.warning("关闭 worker 资源失败", exc_info=True)
    _runtime = None
