"""入库任务（同 Dify tasks/document_indexing_task.py）。"""

from __future__ import annotations

import logging

from ..db.session import get_sessionmaker
from ..indexing.runner import IndexingRunner
from .celery_app import celery_app, get_runtime, run_async

__all__ = ["indexing_task", "rebuild_index_task"]

logger = logging.getLogger(__name__)


@celery_app.task(name="kb.indexing", queue="dataset")
def indexing_task(document_uuids: list[str]) -> None:
    runner = IndexingRunner(get_runtime(), get_sessionmaker())
    for doc_uuid in document_uuids:
        run_async(runner.run(doc_uuid))


@celery_app.task(name="kb.rebuild_index", queue="dataset")
def rebuild_index_task(version_uuid: str) -> None:
    """索引版本重建（换向量模型，§6.2）。中途 worker 被杀会重新投递，从头重建（point 覆盖写）。"""
    from ..indexing.versions import rebuild

    run_async(rebuild(get_runtime(), get_sessionmaker(), version_uuid))
