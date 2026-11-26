"""入库任务（同 Dify tasks/document_indexing_task.py）。

★ 消息里只有文档 / 索引版本的 id：租户从那一行取，进入它再干活（run_queued、rebuild_queued）；
  租户删除后行没了，查不到就结束。不查租户状态（业务系统不知道它）。
"""

from __future__ import annotations

import logging

from ..db.session import get_sessionmaker
from ..document.runner import run_queued
from .celery_app import celery_app, get_runtime, run_async

__all__ = ["indexing_task", "rebuild_index_task"]

logger = logging.getLogger(__name__)


@celery_app.task(name="kb.indexing", queue="dataset")
def indexing_task(document_uuid: str | list[str]) -> None:
    """一个文档一个任务。list：升级前按批投递、还在队列里的消息，拆成一个文档一个任务再投。"""
    if isinstance(document_uuid, list):
        for one in document_uuid:
            indexing_task.delay(one)
        return
    run_async(run_queued(get_runtime(), get_sessionmaker(), document_uuid))


@celery_app.task(name="kb.rebuild_index", queue="dataset")
def rebuild_index_task(version_uuid: str) -> None:
    """索引版本重建（换向量模型，§6.2）。中途 worker 被杀由巡检重新投递，从记下的进度续跑。"""
    from ..index.versions import rebuild_queued

    run_async(rebuild_queued(get_runtime(), get_sessionmaker(), version_uuid))
