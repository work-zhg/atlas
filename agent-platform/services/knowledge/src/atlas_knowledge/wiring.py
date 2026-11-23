"""组合根：登记谁订阅知识库的事件（c4-model/knowledge/code-dataset.html · DatasetEvents）。

知识库领域只发事件、不认识订阅方；订阅关系在这里写一次。

★ 删库时的顺序：各领域先删自己的行，外部资源（向量库的 collection、对象存储的原文件）放在最后——
  前面哪一步失败整个事务回滚，外部资源还在。原文件删失败只记日志，不影响结果。
"""

from __future__ import annotations

from .dataset.events import DatasetCreated, DatasetDeleted, DatasetEvents, MetadataFieldDefined

__all__ = ["dataset_events"]


def dataset_events() -> DatasetEvents:
    from .document import documents
    from .index import index_versions
    from .retrieval import service as retrieval
    from .segment import segments

    bus = DatasetEvents()
    bus.subscribe(DatasetCreated, index_versions.on_dataset_created)
    bus.subscribe(DatasetDeleted, retrieval.on_dataset_deleted)
    bus.subscribe(DatasetDeleted, segments.on_dataset_deleted)
    bus.subscribe(DatasetDeleted, index_versions.on_dataset_deleted)
    bus.subscribe(DatasetDeleted, documents.on_dataset_deleted)
    bus.subscribe(MetadataFieldDefined, index_versions.on_field_defined)
    return bus
