"""应用配置库的行为测试（支线 C，c4-model/infra-platform/code-appconfig.html）。

用真数据库（DATABASE_URL，PG）：登记的 ON CONFLICT、CAS 的 UPDATE … WHERE version 都在 SQL 里，
假实现测不出来。每个用例用自己的 key 前缀，结束时删掉。

验收：两个人同时改同一个 key，后提交的拿到 409；登记时类型不一致，应用启动失败。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from atlas_appconfig import (
    AppConfig,
    ConfigKey,
    ConfigRoutes,
    TypeChanged,
    TypeMismatch,
    UnknownKey,
    ValueKind,
    ValueType,
    VersionConflict,
    upgrade,
)
from atlas_appconfig.db import config_table, revision_table
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
)


# ──────────────────────────────────────────────── 类型（不连库）


@pytest.mark.parametrize(
    ("annotation", "kind", "element"),
    [
        (str, ValueKind.STRING, None),
        (int, ValueKind.INT, None),
        (float, ValueKind.FLOAT, None),
        (bool, ValueKind.BOOL, None),
        (list[str], ValueKind.LIST, ValueKind.STRING),
        (dict[str, int], ValueKind.MAP, ValueKind.INT),
    ],
)
def test_annotations_map_to_value_types(annotation: Any, kind: ValueKind, element: Any) -> None:
    assert ValueType.of(annotation) == ValueType(kind, element)


@pytest.mark.parametrize(
    "annotation", [list[list[str]], dict[int, str], int | None, tuple[int], dict[str, list[int]]]
)
def test_nested_or_odd_types_are_refused(annotation: Any) -> None:
    with pytest.raises(TypeError):
        ValueType.of(annotation)


def test_values_are_checked_strictly() -> None:
    assert ValueType.of(float).validate(3) == 3.0  # int 可以写进 float
    with pytest.raises(TypeMismatch):
        ValueType.of(int).validate(True)  # bool 不算 int
    with pytest.raises(TypeMismatch):
        ValueType.of(list[int]).validate([1, None])
    with pytest.raises(TypeMismatch):
        ValueType.of(dict[str, int]).validate({"a": "1"})
    with pytest.raises(TypeMismatch):
        ValueType.of(str).validate(1)


def test_keys_are_named_and_defaulted_by_code() -> None:
    with pytest.raises(ValueError, match="小写字母"):
        ConfigKey("Sandbox.Max", int, default=1)
    with pytest.raises(TypeMismatch):
        ConfigKey("a.b", int, default="1")


# ──────────────────────────────────────────────── 对着库


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(upgrade)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def prefix(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[str]:
    value = f"t{uuid4().hex[:8]}"
    yield value
    async with sessions() as s:
        ids = sa.select(config_table.c.config_uuid).where(config_table.c.key.like(f"{value}.%"))
        await s.execute(sa.delete(revision_table).where(revision_table.c.config_uuid.in_(ids)))
        await s.execute(sa.delete(config_table).where(config_table.c.key.like(f"{value}.%")))
        await s.commit()


async def test_register_creates_version_one_and_reads_fall_back_to_the_default(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    quota = ConfigKey(f"{prefix}.quota", int, default=10, description="每人几个沙箱")
    images = ConfigKey(f"{prefix}.images", list[str], default=["a/"])
    config = AppConfig(sessions, [quota, images])
    await config.register()
    await config.register()  # 幂等
    assert await config.get(quota) == 10
    assert await config.get_many(quota, images) == (10, ["a/"])
    [entry, *_] = [e for e in await config.list() if e.key is quota]
    assert (entry.item.version, entry.item.value, entry.overridden) == (1, None, False)
    assert [r.version for r in await config.history(quota.name)] == [1]


async def test_concurrent_registration_from_several_instances_is_fine(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    keys = [ConfigKey(f"{prefix}.k{i}", int, default=i) for i in range(5)]
    await asyncio.gather(*(AppConfig(sessions, keys).register() for _ in range(4)))
    assert len(await AppConfig(sessions, keys).list()) == 5


async def test_a_changed_type_fails_registration(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    """验收（C）：库里的类型和代码声明的不一致时抛 TypeChanged，应用启动失败。"""
    await AppConfig(sessions, [ConfigKey(f"{prefix}.x", int, default=1)]).register()
    with pytest.raises(TypeChanged, match="换一个 key 名"):
        await AppConfig(sessions, [ConfigKey(f"{prefix}.x", list[int], default=[])]).register()


async def test_set_bumps_the_version_and_none_restores_the_default(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    quota = ConfigKey(f"{prefix}.quota", int, default=10)
    config = AppConfig(sessions, [quota])
    await config.register()
    entry = await config.set(quota.name, 3, expected_version=1, by="u-1", note="收紧")
    assert (entry.item.version, entry.effective) == (2, 3)
    assert await config.get(quota) == 3  # 不缓存：改了立刻读到
    with pytest.raises(TypeMismatch):
        await config.set(quota.name, "3", expected_version=2, by="u-1")
    await config.set(quota.name, None, expected_version=2, by="u-1")
    assert await config.get(quota) == 10
    history = await config.history(quota.name)
    assert [(r.version, r.value, r.changed_by) for r in history] == [
        (3, None, "u-1"),
        (2, 3, "u-1"),
        (1, None, "system"),
    ]


async def test_two_people_editing_at_once_one_gets_a_conflict(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    """验收（C）：两个人同时改同一个 key，后提交的拿到版本冲突（接口上是 409）。"""
    key = ConfigKey(f"{prefix}.flag", bool, default=False)
    config = AppConfig(sessions, [key])
    await config.register()
    results = await asyncio.gather(
        config.set(key.name, True, expected_version=1, by="alice"),
        config.set(key.name, False, expected_version=1, by="bob"),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["ConfigEntry", "VersionConflict"]
    assert [r.version for r in await config.history(key.name)] == [2, 1]


async def test_rollback_writes_a_new_version(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    key = ConfigKey(f"{prefix}.limits", dict[str, int], default={})
    config = AppConfig(sessions, [key])
    await config.register()
    await config.set(key.name, {"a": 1}, expected_version=1, by="u")
    await config.set(key.name, {"a": 2}, expected_version=2, by="u")
    entry = await config.rollback(key.name, 2, expected_version=3, by="u")
    assert (entry.item.version, entry.effective) == (4, {"a": 1})
    assert (await config.history(key.name))[0].note == "回滚到第 2 版"
    with pytest.raises(VersionConflict):
        await config.rollback(key.name, 1, expected_version=3, by="u")
    with pytest.raises(UnknownKey):
        await config.rollback(key.name, 99, expected_version=4, by="u")


async def test_a_database_failure_is_raised_not_hidden_behind_the_default(prefix: str) -> None:
    """退回默认值会让功能开关在数据库故障时悄悄翻转。"""
    engine = create_async_engine("postgresql+asyncpg://nobody:wrong@127.0.0.1:1/none")
    config = AppConfig(async_sessionmaker(engine), [ConfigKey(f"{prefix}.f", bool, default=True)])
    with pytest.raises(Exception):  # noqa: B017  —— 连不上库的异常类型由驱动决定
        await config.get(ConfigKey(f"{prefix}.f", bool, default=True))
    await engine.dispose()


# ──────────────────────────────────────────────── 管理接口


class Guard:
    def __init__(self, *, edit: bool) -> None:
        self._edit = edit

    async def view(self, request: Request) -> str:
        return "viewer"

    async def edit(self, request: Request) -> str:
        if not self._edit:
            raise HTTPException(403, "不能改配置")
        return "admin"


async def test_routes_list_edit_conflict_and_roll_back(
    sessions: async_sessionmaker[AsyncSession], prefix: str
) -> None:
    key = ConfigKey(f"{prefix}.quota", int, default=10, description="每人几个沙箱")
    config = AppConfig(sessions, [key])
    await config.register()
    app = FastAPI()
    app.include_router(ConfigRoutes(config, Guard(edit=True)).router)
    readonly = FastAPI()
    readonly.include_router(ConfigRoutes(config, Guard(edit=False)).router)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=readonly), base_url="http://t") as ro,
    ):
        listed = (await c.get("/configs")).json()
        mine = next(i for i in listed if i["key"] == key.name)
        assert mine | {"updated_at": None} == {
            "key": key.name,
            "type": "int",
            "description": "每人几个沙箱",
            "default": 10,
            "value": None,
            "effective": 10,
            "overridden": False,
            "version": 1,
            "updated_by": "system",
            "updated_at": None,
        }
        url = f"/configs/{key.name}"
        assert (await ro.put(url, json={"value": 3, "expected_version": 1})).status_code == 403
        r = await c.put(url, json={"value": 3, "expected_version": 1, "note": "收紧"})
        assert r.status_code == 200 and r.json()["effective"] == 3
        assert (await c.put(url, json={"value": 4, "expected_version": 1})).status_code == 409
        assert (await c.put(url, json={"value": "x", "expected_version": 2})).status_code == 422
        assert (await c.get("/configs/no.such")).status_code == 404
        r = await c.post(f"{url}/rollback", json={"to_version": 1, "expected_version": 2})
        assert r.status_code == 200 and r.json()["effective"] == 10
        revisions = (await c.get(f"{url}/revisions")).json()
        assert [(v["version"], v["changed_by"]) for v in revisions] == [
            (3, "admin"),
            (2, "admin"),
            (1, "system"),
        ]
