"""模型平台测试的替身（不叫 helpers / testkit：避免与别处同名模块撞车）。

FakePermissions   用户中心：模型名 → 授权（用户或部门）；编码级授权；用户
"""

from __future__ import annotations

import itertools
from typing import Any

from atlas_modelgateway.errors import Unavailable
from atlas_modelgateway.perms import LEVEL_RANK, Grant, SubjectModels

#: 测试里的用户（用户中心的 uid）
ADMIN = "00000000-0000-7000-8000-00000000000a"
ALICE = "00000000-0000-7000-8000-0000000000a1"
BOB = "00000000-0000-7000-8000-0000000000b0"
GONE = "00000000-0000-7000-8000-0000000000ff"
DEPT = "00000000-0000-7000-8000-0000000000d1"

#: 测试租户（和 gw_testkit 的 TENANT_A 一样）
TENANT = "00000000-0000-0000-0000-00000000000a"
OTHER_TENANT = "00000000-0000-0000-0000-00000000000b"


def as_user(uid: str, tenant: str = TENANT) -> dict[str, str]:
    return {"X-Uid": uid, "X-Tenant-Id": tenant}


def as_app(code: str = "atlas", tenant: str = TENANT) -> dict[str, str]:
    return {"X-Caller-App": code, "X-Tenant-Id": tenant}


class FakePermissions:
    """用户中心的内存替身。ALICE 在部门 DEPT；授给部门的授权覆盖部门里的人（不考虑上下级）。"""

    def __init__(self) -> None:
        #: 模型名 → [(acl id, 级别, 主体类型, 主体 id)]
        self.acls: dict[str, list[tuple[str, str, str, str]]] = {}
        #: 「全部模型」这类编码级授权：用户 → 级别
        self.type_levels: dict[str, str] = {ADMIN: "OWNER"}
        self.users: dict[str, dict[str, Any]] = {
            ADMIN: {"id": ADMIN, "name": "管理员", "status": "active", "dept": DEPT},
            ALICE: {"id": ALICE, "name": "alice", "status": "active", "dept": DEPT},
            BOB: {"id": BOB, "name": "bob", "status": "active", "dept": None},
            GONE: {"id": GONE, "name": "离职的人", "status": "disabled", "dept": None},
        }
        self.down = False
        self.removed: list[str] = []
        self._ids = itertools.count(1)

    def _check(self) -> None:
        if self.down:
            raise Unavailable("用户中心暂不可用", code="AUTHZ_UNAVAILABLE")

    def _covers(self, subject_type: str, subject_id: str, uid: str) -> bool:
        if subject_type == "user":
            return subject_id == uid
        return (self.users.get(uid) or {}).get("dept") == subject_id

    async def usable(self, subject: str) -> SubjectModels:
        self._check()
        names = {
            model
            for model, rows in self.acls.items()
            if any(self._covers(t, sid, subject) for _, _, t, sid in rows)
        }
        typ = self.type_levels.get(subject, "NONE")
        return SubjectModels(frozenset(names), LEVEL_RANK[typ] >= LEVEL_RANK["READ"])

    async def register(self, model: str, display_name: str, owner: str) -> None:
        self._check()
        if model not in self.acls:
            self.acls[model] = [(str(next(self._ids)), "OWNER", "user", owner)]

    async def rename(self, model: str, display_name: str) -> None:
        self._check()

    async def remove(self, model: str) -> None:
        self._check()
        self.acls.pop(model, None)
        self.removed.append(model)

    async def acl(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "id": acl_id,
                "permission": level,
                "subject": {"type": t.upper(), "id": sid, "name": sid},
                "include_sub": False,
            }
            for acl_id, level, t, sid in self.acls.get(model, [])
        ]

    async def grant(self, model: str, operator: str, grants: list[Grant]) -> None:
        self._check()
        rows = self.acls.setdefault(model, [])
        for g in grants:
            rows.append((str(next(self._ids)), g.level, g.subject_type, g.subject_id))

    async def revoke(self, acl_id: str, operator: str) -> None:
        self._check()
        for model, rows in self.acls.items():
            self.acls[model] = [r for r in rows if r[0] != acl_id]

    async def holders(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {"user": {"id": uid}, "permission": "READ"}
            for uid in self.users
            if any(self._covers(t, sid, uid) for _, _, t, sid in self.acls.get(model, []))
        ]

    async def user(self, user_id: str) -> dict[str, Any] | None:
        self._check()
        return self.users.get(user_id)

    async def user_by_account(self, account: str) -> dict[str, Any] | None:
        self._check()
        return next((u for u in self.users.values() if u["name"] == account), None)
