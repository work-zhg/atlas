"""AppConfig：库的入口。宿主用自己的 async_sessionmaker 和声明的 key 构造它，在 lifespan 里
register() 一次。

★ 读不缓存，每次查库：改动立即对所有实例生效，不需要刷新机制。值为空或行还没建时返回代码里的默认值；
  查库失败直接抛出，不退回默认值 —— 退回默认值会让功能开关这类配置在数据库故障时悄悄翻转。
★ key 只能由代码声明：管理接口只能改值，不能新建 key 或改类型。要改类型就换一个 key 名；登记时发现
  类型不一致，直接启动失败（滚动发布时新旧两版实例同时读同一行，原地改类型总有一方读到不合规的值）。
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .key import ConfigKey
from .model import ConfigItem, ConfigRevision
from .repository import ConfigRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["AppConfig", "ConfigEntry", "TypeChanged", "UnknownKey"]


class TypeChanged(TypeError):
    """库里登记的类型和代码声明的不一致。"""


class UnknownKey(LookupError):
    """这个版本的代码没有声明这个 key（或它还没登记）。"""


@dataclass(frozen=True)
class ConfigEntry:
    """一条声明加上它在库里的样子：给管理接口看（默认值、是否已覆盖、生效值）。"""

    key: ConfigKey[Any]
    item: ConfigItem

    @property
    def overridden(self) -> bool:
        return self.item.value is not None

    @property
    def effective(self) -> Any:
        return self.item.effective(self.key.default)


class AppConfig:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], keys: Iterable[ConfigKey[Any]]
    ) -> None:
        self._sessions = sessions
        self._keys: dict[str, ConfigKey[Any]] = {}
        for key in keys:
            if key.name in self._keys:
                raise ValueError(f"配置 {key.name} 声明了两次")
            self._keys[key.name] = key
        self._repo = ConfigRepository()

    @property
    def keys(self) -> dict[str, ConfigKey[Any]]:
        return dict(self._keys)

    async def register(self) -> None:
        """没有的 key 新建一行（版本 1，值为空），已有的刷新说明；类型对不上抛 TypeChanged。"""
        async with self._sessions() as session:
            for key in self._keys.values():
                existing = await self._repo.get(session, key.name)
                if existing is None:
                    item, revision = ConfigItem.registered(key.name, key.type, key.description)
                    if await self._repo.add(session, item, revision):
                        continue
                    existing = await self._repo.get(session, key.name)  # 别的实例刚登记
                    assert existing is not None
                if existing.type != key.type:
                    raise TypeChanged(
                        f"配置 {key.name} 在库里是 {existing.type.describe()}，代码声明的是 "
                        f"{key.type.describe()}：要改类型就换一个 key 名"
                    )
                await self._repo.describe(session, key.name, key.description)
            await session.commit()

    async def get[T](self, key: ConfigKey[T]) -> T:
        async with self._sessions() as session:
            item = await self._repo.get(session, key.name)
        return key.default if item is None else item.effective(key.default)

    async def get_many(self, *keys: ConfigKey[Any]) -> tuple[Any, ...]:
        """热路径上一次取多个：一条查询。"""
        async with self._sessions() as session:
            items = await self._repo.get_many(session, [k.name for k in keys])
        return tuple(
            k.default if (item := items.get(k.name)) is None else item.effective(k.default)
            for k in keys
        )

    async def list(self) -> builtins.list[ConfigEntry]:
        """本版本代码声明的 key（库里有、但代码已经不声明的不列）。"""
        async with self._sessions() as session:
            items = {i.name: i for i in await self._repo.list(session)}
        return [
            ConfigEntry(key, items[name])
            for name, key in sorted(self._keys.items())
            if name in items
        ]

    async def entry(self, name: str) -> ConfigEntry:
        key = self._key(name)
        async with self._sessions() as session:
            item = await self._repo.get(session, name)
        if item is None:
            raise UnknownKey(f"配置 {name} 还没登记")
        return ConfigEntry(key, item)

    async def set(
        self, name: str, value: Any | None, *, expected_version: int, by: str, note: str = ""
    ) -> ConfigEntry:
        """改值（None = 恢复默认值）。类型不对抛 TypeMismatch，版本不对抛 VersionConflict。"""
        key = self._key(name)
        async with self._sessions() as session:
            item = await self._repo.get(session, name)
            if item is None:
                raise UnknownKey(f"配置 {name} 还没登记")
            revision = item.change(value, expected_version=expected_version, by=by, note=note)
            await self._repo.save(session, item, revision, expected_version)
            await session.commit()
        return ConfigEntry(key, item)

    async def history(self, name: str) -> builtins.list[ConfigRevision]:
        self._key(name)
        async with self._sessions() as session:
            return await self._repo.history(session, name)

    async def rollback(
        self, name: str, to_version: int, *, expected_version: int, by: str
    ) -> ConfigEntry:
        """把目标版本的值写成一个新版本，不删除任何历史。"""
        self._key(name)
        async with self._sessions() as session:
            target = await self._repo.revision(session, name, to_version)
        if target is None:
            raise UnknownKey(f"配置 {name} 没有第 {to_version} 版")
        return await self.set(
            name,
            target.value,
            expected_version=expected_version,
            by=by,
            note=f"回滚到第 {to_version} 版",
        )

    def _key(self, name: str) -> ConfigKey[Any]:
        try:
            return self._keys[name]
        except KeyError as exc:
            raise UnknownKey(f"这个版本的代码没有声明配置 {name}") from exc
