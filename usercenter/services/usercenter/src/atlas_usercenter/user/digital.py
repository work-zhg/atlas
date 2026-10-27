"""数字员工（数字员工设计 §3–§4）：接入应用经开放接口创建、维护，一个 Agent 一个。

★ 生命周期跟随来源应用里的对象（Atlas 的 Agent），所以只有**来源应用**能改、停用、启用；
  其他应用只读。管理台只能停用 / 启用（紧急止血）。
★ 创建幂等：(租户, 来源应用, external_ref) 已有就返回已有的 —— 来源应用建一半退出、重试时
  拿到同一个 uid，不会重复。
★ 没有部门、没有邮箱、没有凭据：不进组织树、不能登录。
"""

from __future__ import annotations

import secrets
import string
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import Actor, record
from ..common import require_text
from ..db.models import App, User
from ..errors import Forbidden, Invalid, NotFound

__all__ = ["DigitalEmployeeService", "digital_view"]

#: 账号 = dw- + 10 位短码：全局唯一、稳定；不用 Agent 名（会重名、会改名）。
#: ★ 人的账号不允许出现「-」，两者不会撞
_ACCOUNT_PREFIX = "dw-"
_ALPHABET = string.ascii_lowercase + string.digits
_NAME_MAX = 32


def digital_view(u: User, source_app: App | None = None) -> dict[str, Any]:
    return {
        "id": str(u.uuid),
        "user_type": u.user_type,
        "account": u.account,
        "name": u.name,
        "description": u.description,
        "owner_id": str(u.owner_uuid) if u.owner_uuid else None,
        "source_app": source_app.code if source_app else None,
        "external_ref": u.external_ref,
        "status": u.status,
    }


class DigitalEmployeeService:
    def __init__(self, session: AsyncSession, actor: Actor) -> None:
        self.session = session
        self.actor = actor

    # ───────────────────────────── 读

    async def get(self, uuid: UUID) -> User:
        u = (
            await self.session.execute(
                select(User).where(User.uuid == uuid, User.user_type == "digital")
            )
        ).scalar_one_or_none()
        if u is None:
            raise NotFound("DIGITAL_EMPLOYEE_NOT_FOUND", "数字员工不存在")
        return u

    async def find(self, app: App, external_ref: str) -> User | None:
        return (
            await self.session.execute(
                select(User).where(
                    User.user_type == "digital",
                    User.source_app_uuid == app.uuid,
                    User.external_ref == external_ref,
                )
            )
        ).scalar_one_or_none()

    # ───────────────────────────── 写（只限来源应用）

    async def create(
        self,
        app: App,
        *,
        external_ref: str,
        name: str,
        owner_uuid: UUID,
        description: str | None = None,
    ) -> tuple[User, bool]:
        """→ (数字员工, 是否新建)。"""
        ref = require_text(external_ref, "external_ref", "外部引用", 64)
        existing = await self.find(app, ref)
        if existing is not None:
            return existing, False
        owner = await self._owner(app, owner_uuid)
        user = User(
            account=await self._new_account(),
            name=_name(name),
            user_type="digital",
            owner_uuid=owner.uuid,
            source_app_uuid=app.uuid,
            external_ref=ref,
            description=_description(description),
            status="active",
        )
        self.session.add(user)
        await self.session.flush()
        record(
            self.session,
            self.actor,
            "digital.create",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
            app=app.name,
            external_ref=ref,
            owner=owner.name,
        )
        return user, True

    async def update(
        self,
        app: App,
        uuid: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        owner_uuid: UUID | None = None,
    ) -> User:
        user = await self._own(app, uuid)
        changes: dict[str, list[Any]] = {}
        if name is not None and _name(name) != user.name:
            changes["name"] = [user.name, _name(name)]
            user.name = _name(name)
        if description is not None and _description(description) != user.description:
            changes["description"] = [user.description, _description(description)]
            user.description = _description(description)
        if owner_uuid is not None and owner_uuid != user.owner_uuid:
            owner = await self._owner(app, owner_uuid)
            changes["owner"] = [str(user.owner_uuid), str(owner.uuid)]
            user.owner_uuid = owner.uuid
        if changes:
            user.version += 1
            record(
                self.session,
                self.actor,
                "digital.update",
                "user",
                user.uuid,
                f"{user.name}（{user.account}）",
                changes=changes,
            )
        await self.session.flush()
        return user

    async def set_status(self, app: App, uuid: UUID, status: str) -> User:
        """来源对象归档 → 停用（之后所有判定为无权限）；恢复 → 启用，授权原样还在。"""
        user = await self._own(app, uuid)
        if user.status == status:
            return user
        user.status = status
        user.version += 1
        record(
            self.session,
            self.actor,
            "digital.disable" if status == "disabled" else "digital.enable",
            "user",
            user.uuid,
            f"{user.name}（{user.account}）",
        )
        await self.session.flush()
        return user

    # ───────────────────────────── 内部

    async def _own(self, app: App, uuid: UUID) -> User:
        user = await self.get(uuid)
        if user.source_app_uuid != app.uuid:
            raise Forbidden("NOT_SOURCE_APP", "只有创建它的应用可以修改这个数字员工")
        return user

    async def _owner(self, app: App, owner_uuid: UUID) -> User:
        """负责人：未停用的人，且在调用应用的可访问范围内。"""
        from ..perm.effective import can_access

        owner = (
            await self.session.execute(select(User).where(User.uuid == owner_uuid))
        ).scalar_one_or_none()
        if owner is None:
            raise Invalid("OWNER_NOT_FOUND", "负责人不存在", field="owner_id")
        if owner.is_digital:
            raise Invalid("OWNER_NOT_HUMAN", "负责人必须是人", field="owner_id")
        if owner.status == "disabled":
            raise Invalid("OWNER_DISABLED", "负责人已停用", field="owner_id")
        if not await can_access(self.session, owner, app):
            raise Invalid("OWNER_OUT_OF_SCOPE", "负责人不在本应用的可访问范围内", field="owner_id")
        return owner

    async def _new_account(self) -> str:
        for _ in range(8):
            account = _ACCOUNT_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(10))
            if not await self.session.scalar(select(User.uuid).where(User.account == account)):
                return account
        raise RuntimeError("生成数字员工账号连续冲突")  # 36^10 的空间里不会发生


def _name(raw: str | None) -> str:
    """姓名列 32 字：来源应用给的名字（如「🤖 」+ Agent 名）超长时截断，而不是让创建失败。"""
    v = (raw or "").strip()
    if not v:
        raise Invalid("VALIDATION_FAILED", "请填写名称", field="name")
    return v[:_NAME_MAX]


def _description(raw: str | None) -> str | None:
    return (raw or "").strip()[:200] or None
