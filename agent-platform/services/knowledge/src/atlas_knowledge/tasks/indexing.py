"""入库任务（同 Dify tasks/document_indexing_task.py）。

★ 消息里只有文档 / 索引版本的 id：租户从那一行取，进入它再干活（run_queued、rebuild_queued）。
★ 租户停用了就过一会儿再投一次（不设次数上限）：恢复后接着做；删除后行没了，查不到就结束。
  不靠运营接口的「恢复」钩子重新投递 —— 钩子在租户状态改回可用之前调，那时投出去的任务还会被挡。
"""

from __future__ import annotations

import logging

from ..db.session import get_sessionmaker
from ..indexing.runner import run_queued
from .celery_app import celery_app, get_runtime, run_async

__all__ = ["indexing_task", "rebuild_index_task"]

logger = logging.getLogger(__name__)

#: 租户停用时多久再看一次
SUSPENDED_RETRY_S = 300


@celery_app.task(name="kb.indexing", queue="dataset")
def indexing_task(document_uuids: list[str]) -> None:
    suspended = run_async(run_queued(get_runtime(), get_sessionmaker(), document_uuids))
    if suspended:
        indexing_task.apply_async(args=[suspended], countdown=SUSPENDED_RETRY_S)


@celery_app.task(name="kb.rebuild_index", queue="dataset")
def rebuild_index_task(version_uuid: str) -> None:
    """索引版本重建（换向量模型，§6.2）。中途 worker 被杀会重新投递，从头重建（point 覆盖写）。"""
    from ..indexing.versions import rebuild_queued

    if run_async(rebuild_queued(get_runtime(), get_sessionmaker(), version_uuid)):
        rebuild_index_task.apply_async(args=[version_uuid], countdown=SUSPENDED_RETRY_S)
