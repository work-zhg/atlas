"""记忆的读出形状（记忆设计 §10）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = ["MemoryListOut", "MemoryOut"]


class MemoryOut(BaseModel):
    id: str
    #: 提炼出来的那句事实，例如「用户的包管理器偏好是 pnpm」。
    memory: str
    created_at: str | None = None
    updated_at: str | None = None
    #: 溯源（§03 的 metadata）：这条是从哪个会话、哪一轮提炼出来的。
    #:
    #: ★ 溯源不是锦上添花 —— 自动抽取意味着用户从没主动说过「请记住这个」，
    #:   系统却替他记下了。他要判断一条记忆对不对，首先得能回到它的出处。
    workspace: str | None = None
    source_session: str | None = None
    source_run: str | None = None


class MemoryListOut(BaseModel):
    data: list[MemoryOut]


def to_out(row: dict[str, Any]) -> MemoryOut:
    meta = row.get("metadata") or {}
    return MemoryOut(
        id=str(row.get("id", "")),
        memory=str(row.get("memory", "")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        workspace=meta.get("workspace"),
        source_session=meta.get("source_session"),
        source_run=meta.get("source_run"),
    )
