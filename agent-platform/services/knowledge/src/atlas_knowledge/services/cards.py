"""给智能体看的知识库信息：名称、介绍、能检索的文档数、最近入库时间。

智能体编辑时列可选的库、运行时 knowledge_list 列集合里的库（知识集方案 §3.2）都用它。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Dataset
from ..db.models import Document as DocRow

__all__ = ["dataset_cards"]


async def dataset_cards(session: AsyncSession, datasets: Sequence[Dataset]) -> list[dict[str, Any]]:
    """按传入的顺序。文档数只算检索得到的：入库完成、启用、未归档、当前版本。"""
    stats: dict[Any, tuple[int, Any]] = {}
    if datasets:
        rows = await session.execute(
            select(DocRow.dataset_uuid, func.count(), func.max(DocRow.completed_at))
            .where(
                DocRow.dataset_uuid.in_([d.uuid for d in datasets]),
                DocRow.indexing_status == "completed",
                DocRow.enabled.is_(True),
                DocRow.archived.is_(False),
                DocRow.is_current.is_(True),
            )
            .group_by(DocRow.dataset_uuid)
        )
        stats = {r[0]: (int(r[1]), r[2]) for r in rows}
    out = []
    for d in datasets:
        count, latest = stats.get(d.uuid, (0, None))
        out.append(
            {
                "id": str(d.uuid),
                "name": d.name,
                "description": d.description,
                "icon": d.icon,
                "embedding_provider": d.embedding_provider,
                "embedding_model": d.embedding_model,
                "document_count": count,
                "updated_at": latest.isoformat() if latest else None,
            }
        )
    return out
