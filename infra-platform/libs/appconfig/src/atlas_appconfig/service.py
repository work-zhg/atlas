"""AppConfig：库的入口。宿主用自己的 async_sessionmaker 和声明的 key 构造它，在 lifespan 里
register() 一次。

★ 读不缓存，每次查库：改动立即对所有实例生效，不需要刷新机制。值为空或行还没建时返回代码里的默认值；
  查库失败直接抛出，不退回默认值 —— 退回默认值会让功能开关这类配置在数据库故障时悄悄翻转。
★ key 只能由代码声明：管理接口只能改值，不能新建 key 或改类型。要改类型就换一个 key 名；登记时发现
  类型不一致，直接启动失败（滚动发布时新旧两版实例同时读同一行，原地改类型总有一方读到不合规的值）。
  作用域同理。
★ 租户设置（scope=TENANT）按「本租户的值 → 系统值 → 代码默认值」取；租户由调用方显式传入，库不从
  请求或上下文里推断 —— 没传就抛 ScopeMismatch，不会在没有租户的地方悄悄读成系统值。
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .key import ConfigKey, ConfigScope
from .model import ConfigItem, ConfigRevision
from .repository import ConfigRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["AppConfig", "ConfigEntry", "ScopeMismatch", "TypeChanged", "UnknownKey"]


class TypeChanged(TypeError):
    """库里登记的类型和代码声明的不一致。"""


class UnknownKey(LookupError):
    """这个版本的代码没有声明这个 key（或它还没登记）。"""


class ScopeMismatch(ValueError):
    """作用域和是否给了租户对不上：读租户设置没给租户、按租户改系统参数。"""


@dataclass(frozen=True)
class ConfigEntry:
    """一条声明加上它在库里的样子：给管理接口看（默认值、是否已覆盖、生效值）。

    item 是正在看的那一行（系统值，或某个租户的值）；看租户的值时 inherited 是系统值。
    """

    key: ConfigKey[Any]
    item: ConfigItem
    inherited: ConfigItem | None = None

    @property
    def overridden(self) -> bool:
        return self.item.value is not None

    @property
    def effective(self) -> Any:
        return self.item.effective(self.key.default, self.inherited)


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
                    item, revision = ConfigItem.registered(
                        key.name, key.type, key.description, key.scope
                    )
                    if await self._repo.add(session, item, revision):
                        continue
                    existing = await self._repo.get(session, key.name)  # 别的实例刚登记
                    assert existing is not None
                if existing.type != key.type:
                    raise TypeChanged(
                        f"配置 {key.name} 在库里是 {existing.type.describe()}，代码声明的是 "
                        f"{key.type.describe()}：要改类型就换一个 key 名"
                    )
                if existing.scope != key.scope:
                    raise TypeChanged(
                        f"配置 {key.name} 在库里是 {existing.scope}，代码声明的是 {key.scope}："
                        "要改作用域就换一个 key 名"
                    )
                await self._repo.describe(session, key.name, key.description)
            await session.commit()

    async def get[T](self, key: ConfigKey[T], *, tenant_uuid: UUID | None = None) -> T:
        """系统参数取系统值；租户设置要给租户，按本租户的值 → 系统值 → 默认值取。"""
        (value,) = await self.get_many(key, tenant_uuid=tenant_uuid)
        return value

    async def get_many(
        self, *keys: ConfigKey[Any], tenant_uuid: UUID | None = None
    ) -> tuple[Any, ...]:
        """热路径上一次取多个：一条查询。有租户设置时必须给租户；系统参数不看租户。"""
        if tenant_uuid is None and any(k.scope is ConfigScope.TENANT for k in keys):
            names = [k.name for k in keys if k.scope is ConfigScope.TENANT]
            raise ScopeMismatch(f"读租户设置 {names} 要给出租户")
        async with self._sessions() as session:
            items = await self._repo.get_many(session, [k.name for k in keys], tenant_uuid)
        return tuple(self._effective(k, items, tenant_uuid) for k in keys)

    @staticmethod
    def _effective(
        key: ConfigKey[Any],
        items: dict[tuple[str, UUID | None], ConfigItem],
        tenant_uuid: UUID | None,
    ) -> Any:
        system = items.get((key.name, None))
        own = items.get((key.name, tenant_uuid)) if key.scope is ConfigScope.TENANT else None
        if own is not None:
            return own.effective(key.default, system)
        return key.default if system is None else system.effective(key.default)

    async def list(
        self, scope: ConfigScope | None = None, *, tenant_uuid: UUID | None = None
    ) -> builtins.list[ConfigEntry]:
        """本版本代码声明的 key（库里有、但代码已经不声明的不列）。

        不给租户：系统值（scope 可以只列一种）。给了租户：只列租户设置，显示这个租户的值，
        没改过的也列出来（version = 0），继承的是系统值。
        """
        async with self._sessions() as session:
            system = {i.name: i for i in await self._repo.list(session)}
            own = (
                {i.name: i for i in await self._repo.list(session, tenant_uuid)}
                if tenant_uuid is not None
                else {}
            )
        entries = []
        for name, key in sorted(self._keys.items()):
            if name not in system:
                continue
            if tenant_uuid is not None:
                if key.scope is not ConfigScope.TENANT:
                    continue
                item = own.get(name) or system[name].for_tenant(tenant_uuid)
                entries.append(ConfigEntry(key, item, system[name]))
            elif scope is None or key.scope is scope:
                entries.append(ConfigEntry(key, system[name]))
        return entries

    async def entry(self, name: str, *, tenant_uuid: UUID | None = None) -> ConfigEntry:
        key = self._key(name)
        self._check_tenant(key, tenant_uuid)
        async with self._sessions() as session:
            system = await self._repo.get(session, name)
            own = await self._repo.get(session, name, tenant_uuid) if tenant_uuid else None
        if system is None:
            raise UnknownKey(f"配置 {name} 还没登记")
        if tenant_uuid is None:
            return ConfigEntry(key, system)
        return ConfigEntry(key, own or system.for_tenant(tenant_uuid), system)

    async def set(
        self,
        name: str,
        value: Any | None,
        *,
        tenant_uuid: UUID | None = None,
        expected_version: int,
        by: str,
        note: str = "",
    ) -> ConfigEntry:
        """改值（None = 恢复默认值）。类型不对抛 TypeMismatch，版本不对抛 VersionConflict。

        给了租户就是改这个租户的值（只有租户设置可以）；租户第一次改时期望版本是 0。
        不给租户就是改系统值：所有没改过的租户都跟着变。
        """
        key = self._key(name)
        self._check_tenant(key, tenant_uuid)
        async with self._sessions() as session:
            system = await self._repo.get(session, name)
            if system is None:
                raise UnknownKey(f"配置 {name} 还没登记")
            item = system
            if tenant_uuid is not None:
                item = await self._repo.get(session, name, tenant_uuid) or system.for_tenant(
                    tenant_uuid
                )
            revision = item.change(value, expected_version=expected_version, by=by, note=note)
            await self._repo.save(session, item, revision, expected_version)
            await session.commit()
        return ConfigEntry(key, item, system if tenant_uuid is not None else None)

    async def history(
        self, name: str, *, tenant_uuid: UUID | None = None
    ) -> builtins.list[ConfigRevision]:
        key = self._key(name)
        self._check_tenant(key, tenant_uuid)
        async with self._sessions() as session:
            return await self._repo.history(session, name, tenant_uuid)

    async def rollback(
        self,
        name: str,
        to_version: int,
        *,
        tenant_uuid: UUID | None = None,
        expected_version: int,
        by: str,
    ) -> ConfigEntry:
        """把目标版本的值写成一个新版本，不删除任何历史。"""
        key = self._key(name)
        self._check_tenant(key, tenant_uuid)
        async with self._sessions() as session:
            target = await self._repo.revision(session, name, to_version, tenant_uuid)
        if target is None:
            raise UnknownKey(f"配置 {name} 没有第 {to_version} 版")
        return await self.set(
            name,
            target.value,
            tenant_uuid=tenant_uuid,
            expected_version=expected_version,
            by=by,
            note=f"回滚到第 {to_version} 版",
        )

    @staticmethod
    def _check_tenant(key: ConfigKey[Any], tenant_uuid: UUID | None) -> None:
        if tenant_uuid is not None and key.scope is not ConfigScope.TENANT:
            raise ScopeMismatch(f"配置 {key.name} 是系统参数，没有租户的值")

    def _key(self, name: str) -> ConfigKey[Any]:
        try:
            return self._keys[name]
        except KeyError as exc:
            raise UnknownKey(f"这个版本的代码没有声明配置 {name}") from exc
