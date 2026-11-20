"""管理接口：宿主挂到自己的 FastAPI 应用上（ConfigRoutes(config, guard).router）。

    GET   /configs                        本版本代码声明的 key：类型、默认值、是否已覆盖、生效值、
                                          版本
    GET   /configs/{key}
    PUT   /configs/{key}                  改值（value=null 恢复默认值），必须带期望版本；不对 409
    GET   /configs/{key}/revisions        历史，最新的在前
    POST  /configs/{key}/rollback         把某一版的值写成新版本，必须带期望版本

★ 每个请求先经 ConfigGuard 鉴权、拿到操作人（记进修订历史）。库不认识用户中心和操作码。
★ 值以明文存储、接口里看得到：不能放机密。
★ 宿主挂两份：scope=SYSTEM（默认）列出全部 key 的系统值，守卫只认运营中心；scope=TENANT 只列
  租户设置、显示本租户的值，守卫多实现一个 tenant(request) 回答「这是哪个租户」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .key import ConfigScope
from .model import ConfigRevision, VersionConflict
from .service import AppConfig, ConfigEntry, ScopeMismatch, UnknownKey
from .types import TypeMismatch

__all__ = ["ConfigGuard", "ConfigRoutes", "TenantConfigGuard"]


class ConfigGuard(Protocol):
    """谁能看配置、谁能改配置：返回操作人，没权限就抛 HTTPException(403)。"""

    async def view(self, request: Request) -> str: ...
    async def edit(self, request: Request) -> str: ...


class TenantConfigGuard(ConfigGuard, Protocol):
    """租户设置那一份的守卫：再回答这个请求是哪个租户。库不从请求头推断租户。"""

    async def tenant(self, request: Request) -> UUID: ...


class ConfigOut(BaseModel):
    key: str
    type: str
    scope: str = Field(description="system = 系统参数；tenant = 租户设置")
    description: str
    default: Any
    value: Any | None = Field(description="库里覆盖的值；null = 用默认值（租户设置：用系统值）")
    inherited: Any | None = Field(
        default=None, description="租户设置的系统值（看某个租户的值时才有）"
    )
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
        scope=entry.key.scope.value,
        description=item.description,
        default=entry.key.default,
        value=item.value,
        inherited=entry.inherited.value if entry.inherited is not None else None,
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
    def __init__(
        self,
        config: AppConfig,
        guard: ConfigGuard,
        *,
        scope: ConfigScope = ConfigScope.SYSTEM,
        prefix: str = "/configs",
    ) -> None:
        if scope is ConfigScope.TENANT and not hasattr(guard, "tenant"):
            raise TypeError("租户设置的管理接口要用 TenantConfigGuard（多一个 tenant(request)）")
        self._config = config
        self._guard = guard
        self._scope = scope
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

    async def _tenant(self, request: Request) -> UUID | None:
        if self._scope is not ConfigScope.TENANT:
            return None
        return await cast(TenantConfigGuard, self._guard).tenant(request)

    async def list(self, request: Request) -> list[ConfigOut]:
        await self._guard.view(request)
        tenant = await self._tenant(request)
        return [_out(e) for e in await self._config.list(tenant_uuid=tenant)]

    async def get(self, key: str, request: Request) -> ConfigOut:
        await self._guard.view(request)
        tenant = await self._tenant(request)
        return _out(await _call(self._config.entry(key, tenant_uuid=tenant)))

    async def put(self, key: str, body: SetIn, request: Request) -> ConfigOut:
        by = await self._guard.edit(request)
        tenant = await self._tenant(request)
        entry = await _call(
            self._config.set(
                key,
                body.value,
                tenant_uuid=tenant,
                expected_version=body.expected_version,
                by=by,
                note=body.note,
            )
        )
        return _out(entry)

    async def revisions(self, key: str, request: Request) -> list[RevisionOut]:
        await self._guard.view(request)
        tenant = await self._tenant(request)
        return [_rev(r) for r in await _call(self._config.history(key, tenant_uuid=tenant))]

    async def rollback(self, key: str, body: RollbackIn, request: Request) -> ConfigOut:
        by = await self._guard.edit(request)
        tenant = await self._tenant(request)
        entry = await _call(
            self._config.rollback(
                key,
                body.to_version,
                tenant_uuid=tenant,
                expected_version=body.expected_version,
                by=by,
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
    except (TypeMismatch, ScopeMismatch) as exc:
        raise HTTPException(422, str(exc)) from exc
