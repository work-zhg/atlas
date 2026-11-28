"""FlowSpaces：流程空间的唯一入口（c4-model/teamflow/code-space.html）。

    申请      TeamFlow 自己生成编号、先登记（申请中）并提交，再拿编号向 Agent 平台申请（同一个编号
              重复申请得到同一个）；失败就标已释放、不发起；成功但建流程失败的不回收，删租户时一起释放
    绑定      建流程的事务里绑上流程
    转只读    流程完成或终止：不再放行上传（节点协作也不再开 run）；文件都留着
    文件      列、读、上传、查文件在不在：都转给 Agent 平台，这里只做路径校验和状态判断
    释放      删租户时释放本租户的全部空间

★ 文件的内容和历史都不在 TeamFlow：没有快照，也没有版本。
★ 谁能看、谁能传由入口按团队级别判（只读以上看，读写以上传）；上传记流程事件也由入口做
  （流程空间不依赖流程运行）。
"""

from __future__ import annotations

import posixpath
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agentplatform import AgentPlatform
from ..db.types import utcnow
from ..errors import Conflict, Invalid, NotFound, TFError
from .models import FlowSpace

__all__ = ["FlowSpaces", "SpacePath"]

PATH_MAX = 512


class SpacePath:
    """空间里的路径：相对路径，不含 ..，没有以 . 开头的段（.atlas 这类是 Agent 平台自己用的）。"""

    @staticmethod
    def of(raw: str, *, allow_root: bool = False) -> str:
        p = (raw or "").replace("\\", "/").strip().strip("/")
        if not p:
            if allow_root:
                return ""
            raise Invalid("PATH_INVALID", "路径不能为空")
        if len(p) > PATH_MAX:
            raise Invalid("PATH_INVALID", f"路径不能超过 {PATH_MAX} 个字符")
        parts = p.split("/")
        if any(not s or s.startswith(".") for s in parts):
            raise Invalid("PATH_INVALID", "路径不能含 ..、空段或以 . 开头的段")
        return posixpath.normpath(p)


@dataclass(frozen=True)
class SpaceEntry:
    path: str
    name: str
    is_dir: bool
    size: int | None
    modified_at: str | None
    #: 属于哪个节点：按第一层目录推
    node: str | None


class FlowSpaces:
    def __init__(self, session: AsyncSession, atlas: AgentPlatform) -> None:
        self.session = session
        self.atlas = atlas

    async def get(self, space_uuid: UUID) -> FlowSpace:
        s = await self.session.scalar(select(FlowSpace).where(FlowSpace.uuid == space_uuid))
        if s is None:
            raise NotFound("SPACE_NOT_FOUND", "流程空间不存在")
        return s

    # ───────────────────────────── 申请、绑定、只读、释放

    async def open(self, *, title: str, actor: UUID) -> FlowSpace:
        """先登记并提交，再向 Agent 平台申请。提交会把调用方这之前的写一起提交：调用方要在这之前
        只做读（发起流程时就是这样）。"""
        space = FlowSpace(title=title[:100], created_by=actor)
        self.session.add(space)
        await self.session.commit()
        try:
            await self.atlas.open_workspace(space.uuid, space.title)
        except TFError:
            space.status, space.released_at, space.release_reason = "released", utcnow(), "failed"
            await self.session.commit()
            raise
        space.status, space.applied_at = "active", utcnow()
        await self.session.flush()
        return space

    async def bind(self, space_uuid: UUID, process_uuid: UUID) -> None:
        s = await self.get(space_uuid)
        s.process_uuid = process_uuid

    async def seal(self, space_uuid: UUID) -> None:
        s = await self.get(space_uuid)
        if s.status == "active":
            s.status = "readonly"

    async def release_tenant(self, budget: int) -> int:
        """删租户时调（在这个租户的上下文里）：逐个到 Agent 平台释放，标已释放。
        返回这次释放了几个。"""
        rows = list(
            await self.session.scalars(
                select(FlowSpace).where(FlowSpace.status != "released").limit(budget)
            )
        )
        for s in rows:
            await self.atlas.release_workspace(s.uuid)
            s.status, s.released_at, s.release_reason = "released", utcnow(), "tenant"
        await self.session.flush()
        return len(rows)

    # ───────────────────────────── 文件

    def _readable(self, s: FlowSpace) -> None:
        if s.status not in ("active", "readonly"):
            raise Conflict("SPACE_UNAVAILABLE", "流程空间已释放")

    async def list(self, space_uuid: UUID, dir: str = "") -> dict[str, Any]:
        s = await self.get(space_uuid)
        self._readable(s)
        d = SpacePath.of(dir, allow_root=True)
        got = await self.atlas.list_files(s.uuid, d)
        entries: list[SpaceEntry] = []
        for x in got.get("dirs") or []:
            p = str(x.get("path") or "").strip("/")
            entries.append(
                SpaceEntry(
                    p, x.get("name") or p.rsplit("/", 1)[-1], True, None, None, p.split("/", 1)[0]
                )
            )
        for x in got.get("files") or []:
            p = str(x.get("path") or "").strip("/")
            entries.append(
                SpaceEntry(
                    p,
                    x.get("name") or p.rsplit("/", 1)[-1],
                    False,
                    x.get("size"),
                    x.get("modified_at"),
                    p.split("/", 1)[0] if "/" in p else None,
                )
            )
        return {"dir": d, "entries": entries, "truncated": bool(got.get("truncated"))}

    async def read(self, space_uuid: UUID, path: str) -> tuple[int, AsyncIterator[bytes]]:
        s = await self.get(space_uuid)
        self._readable(s)
        return await self.atlas.download(s.uuid, SpacePath.of(path))

    async def upload(self, space_uuid: UUID, path: str, data: bytes) -> dict[str, Any]:
        s = await self.get(space_uuid)
        if not s.can_write():
            raise Conflict("SPACE_READONLY", "流程已经结束，流程空间只读")
        return await self.atlas.put_file(s.uuid, SpacePath.of(path), data)

    async def exists(self, space_uuid: UUID, paths: list[str]) -> list[str]:
        """→ 不存在的路径。"""
        s = await self.get(space_uuid)
        self._readable(s)
        return await self.atlas.stat_files(s.uuid, [SpacePath.of(p) for p in paths])
