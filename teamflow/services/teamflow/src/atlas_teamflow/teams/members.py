"""TeamMembers：用户中心「团队」数据编码的授权（防腐层）。团队成员和级别只存在那里。

建团队时第一次授权不带操作人（这时还没有 Owner）；之后都以当前用户为操作人，
「至少保留一个 Owner」等规则由用户中心兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..errors import Invalid, NotFound
from ..usercenter import TEAM_CODE, UserCenter

__all__ = ["LEVELS", "TeamMember", "TeamMembers"]

#: 团队级别：Owner = 团队管理员，读写 = 成员，只读 = 只能看
LEVELS = ("OWNER", "WRITE", "READ")


@dataclass(frozen=True)
class TeamMember:
    uid: UUID
    name: str
    account: str | None
    level: str


def _grant(level: str, subject: dict[str, Any]) -> dict[str, Any]:
    if level not in LEVELS:
        raise Invalid("VALIDATION_FAILED", "级别只能是 OWNER、WRITE、READ")
    stype = str(subject.get("type", "USER")).upper()
    if stype not in ("USER", "DEPT"):
        raise Invalid("VALIDATION_FAILED", "成员类型只能是 USER 或 DEPT")
    return {
        "permission": level,
        "subject_type": stype,
        "subject_id": str(subject["id"]),
        "include_sub": bool(subject.get("include_sub")) if stype == "DEPT" else False,
    }


class TeamMembers:
    def __init__(self, uc: UserCenter) -> None:
        self.uc = uc

    async def holders(self, team_uuid: UUID, min_level: str = "READ") -> list[TeamMember]:
        out = []
        for h in await self.uc.holders(TEAM_CODE, team_uuid, min_level):
            u = h["user"]
            out.append(
                TeamMember(UUID(u["id"]), u.get("name") or "", u.get("account"), h["permission"])
            )
        return out

    async def grants(self, team_uuid: UUID) -> list[dict[str, Any]]:
        """授权行（可以是人或部门）：[{id, subject: {type, id, name}, permission, include_sub}]"""
        return await self.uc.acl(TEAM_CODE, team_uuid)

    async def first_grant(
        self, team_uuid: UUID, name: str, owner: UUID, members: list[dict[str, Any]]
    ) -> None:
        grants = [_grant("OWNER", {"type": "USER", "id": owner})]
        for m in members:
            if str(m.get("type", "USER")).upper() == "USER" and str(m["id"]) == str(owner):
                continue
            grants.append(_grant(str(m.get("level") or "WRITE").upper(), m))
        await self.uc.write_grants(TEAM_CODE, team_uuid, name, grants, operator=None)

    async def grant(
        self,
        team_uuid: UUID,
        name: str,
        subjects: list[dict[str, Any]],
        level: str,
        *,
        operator: UUID,
    ) -> None:
        await self.uc.write_grants(
            TEAM_CODE, team_uuid, name, [_grant(level, s) for s in subjects], operator=operator
        )

    async def set_level(self, team_uuid: UUID, acl_id: str, level: str, *, operator: UUID) -> None:
        await self._grant_row(team_uuid, acl_id)
        _grant(level, {"type": "USER", "id": "x"})
        await self.uc.update_grant(acl_id, operator, permission=level)

    async def revoke(self, team_uuid: UUID, acl_id: str, *, operator: UUID) -> dict[str, Any]:
        row = await self._grant_row(team_uuid, acl_id)
        await self.uc.delete_grant(acl_id, operator)
        return row

    async def rename(self, team_uuid: UUID, name: str) -> None:
        await self.uc.rename_data(TEAM_CODE, team_uuid, name)

    async def _grant_row(self, team_uuid: UUID, acl_id: str) -> dict[str, Any]:
        for g in await self.grants(team_uuid):
            if g["id"] == acl_id:
                return g
        raise NotFound("GRANT_NOT_FOUND", "成员授权不存在")

    async def people(self, q: str | None) -> list[dict[str, Any]]:
        return await self.uc.search_users(q)
