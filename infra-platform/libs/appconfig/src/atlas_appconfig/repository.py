"""ConfigRepository：读写 app_config 和 app_config_revision，使用调用方传入的会话。

★ 登记用 INSERT … ON CONFLICT DO NOTHING：多个实例同时启动不会互相冲突。
★ 保存用版本号做 CAS（UPDATE … WHERE version = 期望版本）：没更新到行就抛 VersionConflict；
  修订行在同一个事务里追加。
"""

from __future__ import annotations

import builtins
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from .db import config_table as C
from .db import revision_table as R
from .model import ConfigItem, ConfigRevision, VersionConflict
from .types import ValueKind, ValueType

__all__ = ["ConfigRepository"]


def _item(row: Any) -> ConfigItem:
    element = ValueKind(row.element_kind) if row.element_kind else None
    return ConfigItem(
        name=row.key,
        type=ValueType(ValueKind(row.value_kind), element),
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

        return insert(C).values(**values).on_conflict_do_nothing(constraint="uk_app_config_key")
    if dialect in ("mysql", "mariadb"):
        return sa.insert(C).values(**values).prefix_with("IGNORE")
    return sa.insert(C).values(**values).prefix_with("OR IGNORE")  # sqlite


class ConfigRepository:
    async def get(self, session: AsyncSession, name: str) -> ConfigItem | None:
        row = (await session.execute(sa.select(C).where(C.c.key == name))).one_or_none()
        return _item(row) if row is not None else None

    async def get_many(self, session: AsyncSession, names: list[str]) -> dict[str, ConfigItem]:
        rows = (await session.execute(sa.select(C).where(C.c.key.in_(names)))).all()
        return {row.key: _item(row) for row in rows}

    async def list(self, session: AsyncSession) -> builtins.list[ConfigItem]:
        rows = (await session.execute(sa.select(C).order_by(C.c.key))).all()
        return [_item(row) for row in rows]

    async def add(self, session: AsyncSession, item: ConfigItem, revision: ConfigRevision) -> bool:
        """登记一条（版本 1，值为空）。已经有了（别的实例刚登记）返回 False。"""
        result = await session.execute(
            _insert_ignoring_conflicts(
                session,
                {
                    "config_uuid": item.uuid,
                    "key": item.name,
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
        """按代码刷新说明（不算一个版本）。"""
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
        result = await session.execute(
            sa.update(C)
            .where(C.c.key == item.name, C.c.version == expected_version)
            .values(
                value=item.value,
                version=item.version,
                updated_by=item.updated_by,
                updated_at=item.updated_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            current = await self.get(session, item.name)
            raise VersionConflict(item.name, expected_version, current.version if current else 0)
        await self._append(session, revision)

    async def history(self, session: AsyncSession, name: str) -> builtins.list[ConfigRevision]:
        """最新的在前。"""
        rows = (
            await session.execute(
                sa.select(R)
                .join(C, C.c.config_uuid == R.c.config_uuid)
                .where(C.c.key == name)
                .order_by(R.c.version.desc())
            )
        ).all()
        return [_revision(row) for row in rows]

    async def revision(
        self, session: AsyncSession, name: str, version: int
    ) -> ConfigRevision | None:
        row = (
            await session.execute(
                sa.select(R)
                .join(C, C.c.config_uuid == R.c.config_uuid)
                .where(C.c.key == name, R.c.version == version)
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
