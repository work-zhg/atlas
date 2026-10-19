"""知识库的库级权限 = 用户中心数据权限（数据编码 AtlasKnowledgeBase）。

级别：READ 看到、检索、召回测试 < WRITE 上传、编辑分段、改检索设置 < OWNER 删除、共享。
「全部知识库 · Owner」是用户中心的数据编码级授权（取代原 kb:manage_all 操作码）。

★ 本服务以 Atlas 应用的身份调用户中心开放接口（知识库是 Atlas 的数据，数据编码登记在
  Atlas 应用下）。内网免令牌：请求头 X-Caller-App: atlas，并透传本次调用的 X-Uid /
  X-Tenant-Id（auth.outbound_headers；用户中心据此建立租户上下文）。
★ 文档级权限（主体标签 u:/d:/r:）不在这里：它走 atlas-server 的主体展开，本来就接着用户中心。

三个实现：
  UCDataPermissions   用户中心（uc 模式）
  AllowAll            dev 模式且没配用户中心：人人 OWNER（与 dev 模式操作码全有一致）
  测试里的内存实现      tests/conftest.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .auth import outbound_headers
from .errors import Conflict, Forbidden, Invalid, NotFound

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_CODE",
    "LEVEL_RANK",
    "AllowAll",
    "DataPermissions",
    "Grant",
    "UCDataPermissions",
    "make_data_permissions",
]

DATA_CODE = "AtlasKnowledgeBase"
LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


@dataclass(frozen=True)
class Grant:
    level: str  # READ | WRITE | OWNER
    subject_type: str  # user | dept
    subject_id: str
    include_sub: bool = False


class DataPermissions(Protocol):
    async def level(self, user_id: str, dataset_id: str) -> str:
        """有效级别（含「全部知识库」授权）。"""
        ...

    async def accessible(self, user_id: str, min_level: str = "READ") -> tuple[dict[str, str], str]:
        """→ ({数据 id: 级别}, 全部知识库的级别)。"""
        ...

    async def create(self, dataset_id: str, name: str, grants: list[Grant]) -> None:
        """新库首次写入（须含一条 OWNER）。"""
        ...

    async def acl(self, dataset_id: str) -> list[dict[str, Any]]: ...

    async def grant(self, dataset_id: str, operator: str, grants: list[Grant]) -> None: ...

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None: ...

    async def revoke(self, acl_id: str, operator: str) -> None: ...

    async def rename(self, dataset_id: str, name: str) -> None: ...

    async def cleanup(self, dataset_id: str) -> None: ...

    async def root_dept(self) -> str:
        """根部门 id（「全员可见」= 根部门含下级只读）。"""
        ...


class AllowAll:
    """dev 模式且没接用户中心：人人对一切 OWNER，共享是空操作。"""

    async def level(self, user_id: str, dataset_id: str) -> str:
        return "OWNER"

    async def accessible(self, user_id: str, min_level: str = "READ") -> tuple[dict[str, str], str]:
        return {}, "OWNER"

    async def create(self, dataset_id: str, name: str, grants: list[Grant]) -> None:
        return None

    async def acl(self, dataset_id: str) -> list[dict[str, Any]]:
        return []

    async def grant(self, dataset_id: str, operator: str, grants: list[Grant]) -> None:
        raise Conflict("dev 模式没有接用户中心，不能共享")

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        raise Conflict("dev 模式没有接用户中心，不能共享")

    async def revoke(self, acl_id: str, operator: str) -> None:
        raise Conflict("dev 模式没有接用户中心，不能共享")

    async def rename(self, dataset_id: str, name: str) -> None:
        return None

    async def cleanup(self, dataset_id: str) -> None:
        return None

    async def root_dept(self) -> str:
        return "root"


class UCDataPermissions:
    """用户中心开放接口（/open/v1/data-permissions*）。"""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=10.0, trust_env=False, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            raise _unavailable() from exc
        if r.status_code < 400:
            return r.json() if r.content else None
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        code, message = (
            str(body.get("code", "")),
            str(body.get("message") or "用户中心拒绝了请求"),
        )
        if code in ("OWNER_REQUIRED", "NOT_DATA_OWNER", "LAST_DATA_OWNER"):
            raise Forbidden(message) if code != "LAST_DATA_OWNER" else Conflict(message)
        if r.status_code == 404:
            raise NotFound(message)
        if r.status_code < 500 and r.status_code not in (401, 403):
            raise Invalid(message)
        logger.warning("用户中心拒绝了开放接口调用：%s %s", r.status_code, r.text[:200])
        raise _unavailable()

    async def level(self, user_id: str, dataset_id: str) -> str:
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": DATA_CODE, "data_id": dataset_id, "user_id": user_id},
        )
        return str(r["permission"])

    async def accessible(self, user_id: str, min_level: str = "READ") -> tuple[dict[str, str], str]:
        out: dict[str, str] = {}
        page, type_level = 1, "NONE"
        while True:
            r = await self._call(
                "GET",
                "/open/v1/data-permissions/accessible",
                params={
                    "data_code": DATA_CODE,
                    "user_id": user_id,
                    "min": min_level,
                    "page": page,
                    "size": 500,
                },
            )
            type_level = str(r.get("type_permission") or "NONE")
            out.update({i["data_id"]: i["permission"] for i in r["items"]})
            if len(out) >= int(r.get("total") or 0) or not r["items"]:
                return out, type_level
            page += 1

    def _grants(self, grants: list[Grant]) -> list[dict[str, Any]]:
        return [
            {
                "permission": g.level,
                "subject_type": g.subject_type.upper(),
                "subject_id": g.subject_id,
                "include_sub": g.include_sub,
            }
            for g in grants
        ]

    async def create(self, dataset_id: str, name: str, grants: list[Grant]) -> None:
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": DATA_CODE,
                "data_id": dataset_id,
                "data_name": name,
                "grants": self._grants(grants),
            },
        )

    async def acl(self, dataset_id: str) -> list[dict[str, Any]]:
        try:
            return list(
                await self._call(
                    "GET",
                    "/open/v1/data-permissions/acl",
                    params={"data_code": DATA_CODE, "data_id": dataset_id},
                )
            )
        except NotFound:
            return []

    async def grant(self, dataset_id: str, operator: str, grants: list[Grant]) -> None:
        await self._call(
            "POST",
            "/open/v1/data-permissions/batch",
            json={
                "data_code": DATA_CODE,
                "data_id": dataset_id,
                "operator_id": operator,
                "grants": self._grants(grants),
            },
        )

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        body: dict[str, Any] = {"operator_id": operator}
        if level is not None:
            body["permission"] = level
        if include_sub is not None:
            body["include_sub"] = include_sub
        await self._call("PATCH", f"/open/v1/data-permissions/acl/{acl_id}", json=body)

    async def revoke(self, acl_id: str, operator: str) -> None:
        await self._call(
            "DELETE", f"/open/v1/data-permissions/acl/{acl_id}", params={"operator_id": operator}
        )

    async def rename(self, dataset_id: str, name: str) -> None:
        try:
            await self._call(
                "PATCH",
                "/open/v1/data-objects",
                json={"data_code": DATA_CODE, "data_id": dataset_id, "data_name": name},
            )
        except NotFound:  # 没登记（服务账号的库）：无事可做
            return

    async def cleanup(self, dataset_id: str) -> None:
        await self._call(
            "DELETE",
            "/open/v1/data-permissions",
            params={"data_code": DATA_CODE, "data_id": dataset_id},
        )

    async def root_dept(self) -> str:
        tree = await self._call("GET", "/open/v1/depts/tree")
        return next(str(d["id"]) for d in tree if not d.get("parent_id"))


def _unavailable() -> Exception:
    from .errors import Unavailable

    return Unavailable("用户中心暂不可用，请稍后重试")


def make_data_permissions(settings: Any) -> DataPermissions:
    """uc 模式接用户中心；dev 模式不接（人人 OWNER）。"""
    if settings.auth_mode == "uc":
        return UCDataPermissions(settings.uc_base_url)
    return AllowAll()
