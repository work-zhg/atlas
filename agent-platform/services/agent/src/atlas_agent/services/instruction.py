"""持久指令（doc/context-management-design.html §10）：平台（管理员）与用户（个人偏好）两层。

    管理接口  api/v1/instructions.py（个人的自己改；平台的要 model:manage）
    运行时    executor/context.py 每轮开始时读一次（for_user）

★ 每层有长度上限：指令每轮都进上下文，塞太多既贵又稀释注意力。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Instruction

__all__ = ["MAX_CHARS", "PLATFORM", "USER", "InstructionService", "owner_of"]

PLATFORM = "platform"
USER = "user"
#: 每层的字符上限（约 4k tokens）
MAX_CHARS = 10_000


def owner_of(scope: str, uid: UUID) -> str:
    """平台指令只有一份（owner 为空串）；个人指令一人一份。"""
    return "" if scope == PLATFORM else str(uid)


class InstructionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, scope: str, owner: str) -> Instruction | None:
        return await self.session.get(Instruction, (scope, owner))

    async def put(self, scope: str, owner: str, content: str, *, actor: UUID) -> Instruction:
        """整段覆盖（不提交，由调用方提交）。"""
        row = await self.get(scope, owner)
        if row is None:
            row = Instruction(scope=scope, owner_id=owner)
            self.session.add(row)
        row.content = content
        row.updated_by = actor
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def for_user(self, user_id: UUID | str) -> tuple[str, str]:
        """运行时：(平台指令, 该用户的个人指令)，没有的为空串。"""
        rows = await self.session.scalars(
            select(Instruction).where(
                ((Instruction.scope == PLATFORM) & (Instruction.owner_id == ""))
                | ((Instruction.scope == USER) & (Instruction.owner_id == str(user_id)))
            )
        )
        got = {r.scope: r.content for r in rows}
        return got.get(PLATFORM, ""), got.get(USER, "")
