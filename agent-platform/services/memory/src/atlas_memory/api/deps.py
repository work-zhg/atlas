"""接口依赖：会话、数据权限、实例访问。

`access(级别)` 是每个实例接口的入口：按路径里的作用域编码 + 关联主键找到实例；作用域打开了
数据权限开关时，按请求头里的调用用户在用户中心校验级别（§3.7）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Caller, caller_of
from ..db.models import Instance, Scope
from ..db.session import get_session
from ..errors import Forbidden, Unauthorized
from ..keys import parse_key
from ..perms import LEVEL_RANK, DataPermissions
from ..settings import MemorySettings, get_settings
from ..store import MemoryStore

__all__ = ["Access", "PermsDep", "SessionDep", "StoreDep", "access"]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_perms(request: Request) -> DataPermissions:
    """进程内单例（app 生命周期里建好；测试注入内存实现）。"""
    return request.app.state.perms  # type: ignore[no-any-return]


PermsDep = Annotated[DataPermissions, Depends(get_perms)]


def get_store(
    session: SessionDep, settings: Annotated[MemorySettings, Depends(get_settings)]
) -> MemoryStore:
    return MemoryStore(
        session, entry_max_chars=settings.entry_max_chars, max_removes=settings.write_max_removes
    )


StoreDep = Annotated[MemoryStore, Depends(get_store)]


@dataclass
class Access:
    store: MemoryStore
    scope: Scope
    key: dict[str, str]
    #: 未建的实例为 None（开关关闭的作用域：读到空目录，第一次写入时建）
    instance: Instance | None
    caller: Caller | None

    async def locked(self) -> Instance:
        """写操作：锁住实例行（不存在时建）。之后的读写都在这把锁里。"""
        inst = await self.store.lock_instance(self.scope, self.key, create=True)
        assert inst is not None
        self.instance = inst
        return inst

    async def locked_existing(self) -> Instance | None:
        inst = await self.store.lock_instance(self.scope, self.key, create=False)
        self.instance = inst
        return inst


async def check_level(
    perms: DataPermissions, scope: Scope, inst: Instance | None, caller: Caller | None, need: str
) -> None:
    """数据权限开关打开时：调用用户对这个实例的级别要够。没登记的实例一律拒绝。"""
    if not scope.check_permission:
        return
    if caller is None:
        raise Unauthorized("该作用域校验数据权限，请求要带调用用户（X-Uid）")
    if inst is None or not inst.uc_registered:
        raise Forbidden("实例还没有登记归属，不能访问", code="NOT_REGISTERED")
    assert scope.data_code
    level = await perms.level(caller.auth_uid, scope.data_code, str(inst.uuid))
    if LEVEL_RANK.get(level, 0) < LEVEL_RANK[need]:
        raise Forbidden("没有访问该实例的权限", code="NO_PERMISSION", need=need, level=level)


def access(need: str) -> Callable[..., Awaitable[Access]]:
    async def dep(
        scope: str, key: str, request: Request, store: StoreDep, perms: PermsDep
    ) -> Access:
        sc = await store.scope(scope)
        parsed = parse_key(sc.key_fields, key)
        inst = await store.find_instance(sc, parsed)
        caller = caller_of(request)
        await check_level(perms, sc, inst, caller, need)
        return Access(store=store, scope=sc, key=parsed, instance=inst, caller=caller)

    return dep


ReadAccess = Annotated[Access, Depends(access("READ"))]
WriteAccess = Annotated[Access, Depends(access("WRITE"))]
OwnerAccess = Annotated[Access, Depends(access("OWNER"))]
