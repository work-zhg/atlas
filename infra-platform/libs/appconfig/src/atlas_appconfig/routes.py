"""管理接口：宿主挂到自己的 FastAPI 应用上（ConfigRoutes(config, guard).router）。

    GET   /configs                        本版本代码声明的 key：类型、默认值、是否已覆盖、生效值、
                                          版本
    GET   /configs/{key}
    PUT   /configs/{key}                  改值（value=null 恢复默认值），必须带期望版本；不对 409
    GET   /configs/{key}/revisions        历史，最新的在前
    POST  /configs/{key}/rollback         把某一版的值写成新版本，必须带期望版本

★ 每个请求先经 ConfigGuard 鉴权、拿到操作人（记进修订历史）。库不认识用户中心和操作码。
★ 值以明文存储、接口里看得到：不能放机密。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .model import ConfigRevision, VersionConflict
from .service import AppConfig, ConfigEntry, UnknownKey
from .types import TypeMismatch

__all__ = ["ConfigGuard", "ConfigRoutes"]


class ConfigGuard(Protocol):
    """谁能看配置、谁能改配置：返回操作人，没权限就抛 HTTPException(403)。"""

    async def view(self, request: Request) -> str: ...
    async def edit(self, request: Request) -> str: ...


class ConfigOut(BaseModel):
    key: str
    type: str
    description: str
    default: Any
    value: Any | None = Field(description="库里覆盖的值；null = 用默认值")
    effective: Any
    overridden: bool
    version: int
    updated_by: str
    updated_at: datetime


class RevisionOut(BaseModel):
    version: int
    value: Any | None
    note: str
    changed_by: str
    created_at: datetime


class SetIn(BaseModel):
    value: Any | None = None
    expected_version: int
    note: str = Field(default="", max_length=500)


class RollbackIn(BaseModel):
    to_version: int
    expected_version: int


def _out(entry: ConfigEntry) -> ConfigOut:
    item = entry.item
    return ConfigOut(
        key=item.name,
        type=item.type.describe(),
        description=item.description,
        default=entry.key.default,
        value=item.value,
        effective=entry.effective,
        overridden=entry.overridden,
        version=item.version,
        updated_by=item.updated_by,
        updated_at=item.updated_at,
    )


def _rev(r: ConfigRevision) -> RevisionOut:
    return RevisionOut(
        version=r.version,
        value=r.value,
        note=r.note,
        changed_by=r.changed_by,
        created_at=r.created_at,
    )


class ConfigRoutes:
    def __init__(self, config: AppConfig, guard: ConfigGuard, *, prefix: str = "/configs") -> None:
        self._config = config
        self._guard = guard
        self.router = APIRouter(prefix=prefix, tags=["app-config"])
        self.router.add_api_route("", self.list, methods=["GET"], response_model=list[ConfigOut])
        self.router.add_api_route("/{key}", self.get, methods=["GET"], response_model=ConfigOut)
        self.router.add_api_route("/{key}", self.put, methods=["PUT"], response_model=ConfigOut)
        self.router.add_api_route(
            "/{key}/revisions", self.revisions, methods=["GET"], response_model=list[RevisionOut]
        )
        self.router.add_api_route(
            "/{key}/rollback", self.rollback, methods=["POST"], response_model=ConfigOut
        )

    async def list(self, request: Request) -> list[ConfigOut]:
        await self._guard.view(request)
        return [_out(e) for e in await self._config.list()]

    async def get(self, key: str, request: Request) -> ConfigOut:
        await self._guard.view(request)
        return _out(await _call(self._config.entry(key)))

    async def put(self, key: str, body: SetIn, request: Request) -> ConfigOut:
        by = await self._guard.edit(request)
        entry = await _call(
            self._config.set(
                key, body.value, expected_version=body.expected_version, by=by, note=body.note
            )
        )
        return _out(entry)

    async def revisions(self, key: str, request: Request) -> list[RevisionOut]:
        await self._guard.view(request)
        return [_rev(r) for r in await _call(self._config.history(key))]

    async def rollback(self, key: str, body: RollbackIn, request: Request) -> ConfigOut:
        by = await self._guard.edit(request)
        entry = await _call(
            self._config.rollback(
                key, body.to_version, expected_version=body.expected_version, by=by
            )
        )
        return _out(entry)


async def _call(coro: Any) -> Any:
    try:
        return await coro
    except UnknownKey as exc:
        raise HTTPException(404, str(exc)) from exc
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except TypeMismatch as exc:
        raise HTTPException(422, str(exc)) from exc
