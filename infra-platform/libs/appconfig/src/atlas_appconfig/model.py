"""ConfigItem（聚合根）和 ConfigRevision（实体）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .db import uuid7
from .types import ValueType

__all__ = ["SYSTEM", "ConfigItem", "ConfigRevision", "VersionConflict"]

#: 登记时的操作人
SYSTEM = "system"


class VersionConflict(RuntimeError):
    """期望版本和当前版本对不上：别人先改了。"""

    def __init__(self, name: str, expected: int, current: int) -> None:
        super().__init__(f"配置 {name} 已经是第 {current} 版，不是期望的第 {expected} 版")
        self.name, self.expected, self.current = name, expected, current


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ConfigRevision:
    """一条配置的一个版本，只追加。回滚就是把旧版本的值再写成一个新版本。"""

    config_uuid: UUID
    version: int
    value: Any | None
    note: str
    changed_by: str
    created_at: datetime = field(default_factory=_now)
    uuid: UUID = field(default_factory=uuid7)


@dataclass
class ConfigItem:
    """库里的一条配置。值为空表示没有覆盖、用代码里的默认值；类型登记后不能再改。"""

    name: str
    type: ValueType
    value: Any | None = None
    version: int = 1
    description: str = ""
    updated_by: str = SYSTEM
    updated_at: datetime = field(default_factory=_now)
    created_at: datetime = field(default_factory=_now)
    uuid: UUID = field(default_factory=uuid7)

    @classmethod
    def registered(
        cls, name: str, type: ValueType, description: str
    ) -> tuple[ConfigItem, ConfigRevision]:
        item = cls(name=name, type=type, description=description)
        return item, ConfigRevision(item.uuid, 1, None, "登记", SYSTEM, item.created_at)

    def change(
        self, value: Any | None, *, expected_version: int, by: str, note: str = ""
    ) -> ConfigRevision:
        """改值（None = 恢复默认值）：先校验期望版本和类型，再把版本加一并产生这一版的修订。"""
        if expected_version != self.version:
            raise VersionConflict(self.name, expected_version, self.version)
        checked = None if value is None else self.type.validate(value)
        self.value, self.version, self.updated_by, self.updated_at = (
            checked,
            self.version + 1,
            by,
            _now(),
        )
        return ConfigRevision(self.uuid, self.version, checked, note, by, self.updated_at)

    def effective[T](self, default: T) -> T:
        return default if self.value is None else self.type.validate(self.value)
