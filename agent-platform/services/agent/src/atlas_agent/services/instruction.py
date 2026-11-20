"""持久指令（doc/context-management-design.html §10）：租户（租户管理员）与用户（个人偏好）两层。

    管理接口  api/v1/instructions.py（个人的自己改；租户的要 settings:manage）
    运行时    executor/context.py 每轮开始时读一次（for_user）

★ 每层有长度上限：指令每轮都进上下文，塞太多既贵又稀释注意力。
★ 没有平台租户（c4-model/agent/code-tenant-settings.html）：原来的平台指令成了每个租户一份的租户
  指令（迁移 0049 把 scope 从 platform 改成 tenant）；instruction 是租户表，按租户自动过滤。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Instruction
from ..db.session import fetch_one

__all__ = ["MAX_CHARS", "TENANT", "USER", "InstructionService", "owner_of"]

TENANT = "tenant"
USER = "user"
#: 每层的字符上限（约 4k tokens）
MAX_CHARS = 10_000


def owner_of(scope: str, uid: UUID) -> str:
    """租户指令每个租户一份（owner 为空串）；个人指令一人一份。"""
    return "" if scope == TENANT else str(uid)


class InstructionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, scope: str, owner: str) -> Instruction | None:
        return await fetch_one(
            self.session, Instruction, Instruction.scope == scope, Instruction.owner_id == owner
        )

    async def put(self, scope: str, owner: str, content: str, *, actor: UUID) -> Instruction:
        """整段覆盖（不提交，由调用方提交）。"""
        row = await self.get(scope, owner)
        if row is None:
            row = Instruction(scope=scope, owner_id=owner)
            self.session.add(row)
        row.content = content
        row.updated_by_uuid = actor
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def for_user(self, user_id: UUID | str) -> tuple[str, str]:
        """运行时：(租户指令, 该用户的个人指令)，没有的为空串。"""
        rows = await self.session.scalars(
            select(Instruction).where(
                ((Instruction.scope == TENANT) & (Instruction.owner_id == ""))
                | ((Instruction.scope == USER) & (Instruction.owner_id == str(user_id)))
            )
        )
        got = {r.scope: r.content for r in rows}
        return got.get(TENANT, ""), got.get(USER, "")
