"""审计事件：只追加，与业务写入同一事务提交（总体设计 §11）。

★ detail 里不放密码、Secret；邮箱 / 手机由调用方脱敏后再传。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AuditEvent

__all__ = ["Actor", "mask_email", "mask_phone", "record"]


class Actor:
    """操作人：管理台用户、接入应用、运营中心或系统。

    接入应用经内网请求头调用时，on_behalf = 请求头 X-Uid（谁在用），写进审计 detail.uid。
    运营中心经运营接口调用时，uuid = 运营账号（不是任何租户里的人），detail 里记运营人员的名字
    和运营中心的租户操作，两边的审计能对上。
    """

    def __init__(
        self,
        kind: str,
        uuid: UUID | None = None,
        on_behalf: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.uuid = uuid
        self.on_behalf = on_behalf
        self.extra = extra or {}

    @classmethod
    def user(cls, uuid: UUID) -> Actor:
        return cls("user", uuid)

    @classmethod
    def app(cls, uuid: UUID, on_behalf: str | None = None) -> Actor:
        return cls("app", uuid, on_behalf)

    @classmethod
    def system(cls) -> Actor:
        return cls("system")

    @classmethod
    def ops(cls, operator: UUID | None, name: str = "", operation: UUID | None = None) -> Actor:
        extra = {"operator": name or None, "operation": operation}
        return cls("ops", operator, extra={k: v for k, v in extra.items() if v is not None})

    @property
    def user_uuid(self) -> UUID | None:
        return self.uuid if self.kind == "user" else None


def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    target_type: str,
    target_uuid: UUID | None = None,
    target_name: str | None = None,
    **detail: Any,
) -> None:
    clean = {k: _jsonable(v) for k, v in detail.items() if v is not None}
    if actor.on_behalf:
        clean.setdefault("uid", actor.on_behalf)
    for k, v in actor.extra.items():
        clean.setdefault(k, _jsonable(v))
    session.add(
        AuditEvent(
            actor_type=actor.kind,
            actor_uuid=actor.uuid,
            action=action,
            target_type=target_type,
            target_uuid=target_uuid,
            target_name=target_name,
            detail=clean or None,
        )
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return value
    name, _, domain = value.partition("@")
    return f"{name[:1]}***@{domain}"


def mask_phone(value: str | None) -> str | None:
    if not value or len(value) < 7:
        return value
    return f"{value[:3]}****{value[-4:]}"
