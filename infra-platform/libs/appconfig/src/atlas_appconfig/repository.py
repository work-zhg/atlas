"""ConfigRepository：读写 app_config 和 app_config_revision，使用调用方传入的会话。

★ 登记用 INSERT … ON CONFLICT DO NOTHING：多个实例同时启动不会互相冲突。
★ 保存用版本号做 CAS（UPDATE … WHERE version = 期望版本）：没更新到行就抛 VersionConflict；
  修订行在同一个事务里追加。租户的值第一次写入是 INSERT，两个人同时第一次写，后到的冲突。
★ 一个 key 有一行系统值，再加改过它的每个租户各一行；tenant_uuid=None 指系统值（库里存
  SYSTEM_ROW，见 db.py），只在这一层换算。
"""

from __future__ import annotations

import builtins
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SYSTEM_ROW
from .db import config_table as C
from .db import revision_table as R
from .key import ConfigScope
from .model import ConfigItem, ConfigRevision, VersionConflict
from .types import ValueKind, ValueType

__all__ = ["ConfigRepository"]


def _row_tenant(tenant_uuid: UUID | None) -> UUID:
    return SYSTEM_ROW if tenant_uuid is None else tenant_uuid


def _item(row: Any) -> ConfigItem:
    element = ValueKind(row.element_kind) if row.element_kind else None
    return ConfigItem(
        name=row.key,
        type=ValueType(ValueKind(row.value_kind), element),
        scope=ConfigScope(row.scope),
        tenant_uuid=None if row.tenant_uuid == SYSTEM_ROW else row.tenant_uuid,
        value=row.value,
        version=row.version,
        description=row.description,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        created_at=row.created_at,
        uuid=row.config_uuid,
    )


def _revision(row: Any) -> ConfigRevision:
    return ConfigRevision(
        config_uuid=row.config_uuid,
        version=row.version,
        value=row.value,
        note=row.note,
        changed_by=row.changed_by,
        created_at=row.created_at,
        uuid=row.revision_uuid,
    )


def _insert_ignoring_conflicts(session: AsyncSession, values: dict[str, Any]) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        return (
            insert(C).values(**values).on_conflict_do_nothing(constraint="uk_app_config_key_tenant")
        )
    if dialect in ("mysql", "mariadb"):
        return sa.insert(C).values(**values).prefix_with("IGNORE")
    return sa.insert(C).values(**values).prefix_with("OR IGNORE")  # sqlite


def _of(name: str, tenant_uuid: UUID | None) -> Any:
    return sa.and_(C.c.key == name, C.c.tenant_uuid == _row_tenant(tenant_uuid))


class ConfigRepository:
    async def get(
        self, session: AsyncSession, name: str, tenant_uuid: UUID | None = None
    ) -> ConfigItem | None:
        row = (await session.execute(sa.select(C).where(_of(name, tenant_uuid)))).one_or_none()
        return _item(row) if row is not None else None

    async def get_many(
        self, session: AsyncSession, names: list[str], tenant_uuid: UUID | None = None
    ) -> dict[tuple[str, UUID | None], ConfigItem]:
        """系统值，以及给了租户时这个租户的值，一条查询。键是 (key, 租户或 None)。"""
        owners = [SYSTEM_ROW] if tenant_uuid is None else [SYSTEM_ROW, tenant_uuid]
        rows = (
            await session.execute(
                sa.select(C).where(C.c.key.in_(names), C.c.tenant_uuid.in_(owners))
            )
        ).all()
        items = (_item(row) for row in rows)
        return {(i.name, i.tenant_uuid): i for i in items}

    async def list(
        self, session: AsyncSession, tenant_uuid: UUID | None = None
    ) -> builtins.list[ConfigItem]:
        """系统值；给了租户就只列这个租户的值。"""
        rows = (
            await session.execute(
                sa.select(C)
                .where(C.c.tenant_uuid == _row_tenant(tenant_uuid))
                .order_by(C.c.key)
            )
        ).all()
        return [_item(row) for row in rows]

    async def add(self, session: AsyncSession, item: ConfigItem, revision: ConfigRevision) -> bool:
        """新建一行（系统值登记时；租户的值第一次写入时）。已经有了返回 False。"""
        result = await session.execute(
            _insert_ignoring_conflicts(
                session,
                {
                    "config_uuid": item.uuid,
                    "key": item.name,
                    "scope": item.scope.value,
                    "tenant_uuid": _row_tenant(item.tenant_uuid),
                    "value_kind": item.type.kind.value,
                    "element_kind": item.type.element.value if item.type.element else None,
                    "value": item.value,
                    "version": item.version,
                    "description": item.description,
                    "updated_by": item.updated_by,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                },
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            return False
        await self._append(session, revision)
        return True

    async def describe(self, session: AsyncSession, name: str, description: str) -> None:
        """按代码刷新说明（不算一个版本）。系统值和各租户的值都刷新。"""
        await session.execute(
            sa.update(C)
            .where(C.c.key == name, C.c.description != description)
            .values(description=description)
        )

    async def save(
        self,
        session: AsyncSession,
        item: ConfigItem,
        revision: ConfigRevision,
        expected_version: int,
    ) -> None:
        if expected_version == 0:
            # 租户的值第一次写入：建行。两个人同时第一次写，后到的冲突
            if not await self.add(session, item, revision):
                current = await self.get(session, item.name, item.tenant_uuid)
                raise VersionConflict(item.name, 0, current.version if current else 0)
            return
        result = await session.execute(
            sa.update(C)
            .where(_of(item.name, item.tenant_uuid), C.c.version == expected_version)
            .values(
                value=item.value,
                version=item.version,
                updated_by=item.updated_by,
                updated_at=item.updated_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            current = await self.get(session, item.name, item.tenant_uuid)
            raise VersionConflict(item.name, expected_version, current.version if current else 0)
        await self._append(session, revision)

    async def history(
        self, session: AsyncSession, name: str, tenant_uuid: UUID | None = None
    ) -> builtins.list[ConfigRevision]:
        """最新的在前。"""
        rows = (
            await session.execute(
                sa.select(R)
                .join(C, C.c.config_uuid == R.c.config_uuid)
                .where(_of(name, tenant_uuid))
                .order_by(R.c.version.desc())
            )
        ).all()
        return [_revision(row) for row in rows]

    async def revision(
        self, session: AsyncSession, name: str, version: int, tenant_uuid: UUID | None = None
    ) -> ConfigRevision | None:
        row = (
            await session.execute(
                sa.select(R)
                .join(C, C.c.config_uuid == R.c.config_uuid)
                .where(_of(name, tenant_uuid), R.c.version == version)
            )
        ).one_or_none()
        return _revision(row) if row is not None else None

    async def _append(self, session: AsyncSession, revision: ConfigRevision) -> None:
        await session.execute(
            sa.insert(R).values(
                revision_uuid=revision.uuid,
                config_uuid=revision.config_uuid,
                version=revision.version,
                value=revision.value,
                note=revision.note,
                changed_by=revision.changed_by,
                created_at=revision.created_at,
            )
        )
