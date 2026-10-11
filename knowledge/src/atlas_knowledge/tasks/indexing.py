"""入库任务（同 Dify tasks/document_indexing_task.py）。"""

from __future__ import annotations

import logging

from ..db.session import get_sessionmaker
from ..indexing.runner import IndexingRunner
from .celery_app import celery_app, get_runtime, run_async

__all__ = ["indexing_task"]

logger = logging.getLogger(__name__)


@celery_app.task(name="kb.indexing", queue="dataset")
def indexing_task(document_uuids: list[str]) -> None:
    runner = IndexingRunner(get_runtime(), get_sessionmaker())
    for doc_uuid in document_uuids:
        run_async(runner.run(doc_uuid))
