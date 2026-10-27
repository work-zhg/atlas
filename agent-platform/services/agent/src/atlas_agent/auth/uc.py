"""用户中心开放接口客户端（/open/v1）。Atlas 与用户中心的唯一接触点（权限设计 §4、§5）。

★ 内网免令牌（内网身份传递 §3、§11）：应用身份 = 请求头 X-Caller-App: atlas；租户与「谁在用」
  原样透传本次调用的 X-Tenant-Id / X-Uid（outbound.py）。用户中心按它们建立租户上下文。
★ 测试里用内存实现替换，业务代码只依赖这里的方法签名。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from ..config import Settings
from ..errors import AppError
from ..outbound import outbound_headers

__all__ = ["UCClient", "UCError"]


class UCError(AppError):
    """用户中心返回的业务错误，或用户中心不可用。"""

    status_code = 502

    def __init__(self, kind: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


class UCClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        # 用户中心在内网 / 本机：不走代理
        self._http = httpx.AsyncClient(
            base_url=settings.uc_base_url, timeout=10.0, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            raise UCError("uc_unavailable", "用户中心暂不可用，请稍后重试", 503) from exc
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                raise UCError("uc_error", f"用户中心返回 {r.status_code}") from None
            status = r.status_code if r.status_code in (401, 403, 404, 409, 422) else 502
            raise UCError(
                str(body.get("code", "uc_error")).lower(),
                str(body.get("message", "用户中心返回错误")),
                status,
            )
        return r.json()

    async def authz(self, user_id: UUID) -> dict[str, Any]:
        """授权快照：{can_access, roles, permissions, menus}（不在应用可访问范围时全空）。"""
        return await self._call("GET", f"/open/v1/users/{user_id}/authz")

    async def data_type_level(self, user_id: UUID, data_code: str) -> dict[str, Any]:
        """数据编码级授权：{permission, sources} —— 用户对该编码下全部数据的级别。"""
        return await self._call(
            "GET",
            "/open/v1/data-permissions/type",
            params={"data_code": data_code, "user_id": str(user_id)},
        )

    async def user(self, user_id: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/users/{user_id}")

    async def search_users(
        self, q: str | None, *, size: int = 20, digital: bool = False
    ) -> list[dict[str, Any]]:
        """选人：本应用可访问范围内、未停用的用户 → [{id, user_type, account, name, dept_id}]。

        digital=True 连数字员工一起（共享知识库给 Agent 时要能选到它）。
        """
        params: dict[str, Any] = {"q": q or "", "size": size}
        if digital:
            params["types"] = "human,digital"
        r = await self._call("GET", "/open/v1/users", params=params)
        return list(r["items"])

    async def depts_tree(self) -> list[dict[str, Any]]:
        """→ [{id, parent_id, name, leader_id}]（全部部门，按层级排序）"""
        return await self._call("GET", "/open/v1/depts/tree")

    # ───────────────────────────── 数字员工（数字员工设计 §4）

    async def create_digital_employee(
        self, *, external_ref: str, name: str, owner_id: UUID, description: str | None = None
    ) -> dict[str, Any]:
        """幂等：同一 external_ref（agent id）重复调用返回同一个 → {id, account, name, status…}"""
        return await self._call(
            "POST",
            "/open/v1/digital-employees",
            json={
                "external_ref": external_ref,
                "name": name,
                "owner_id": str(owner_id),
                "description": description,
            },
        )

    async def digital_employee(self, uid: UUID) -> dict[str, Any]:
        return await self._call("GET", f"/open/v1/digital-employees/{uid}")

    async def update_digital_employee(self, uid: UUID, **fields: Any) -> dict[str, Any]:
        body = {k: (str(v) if isinstance(v, UUID) else v) for k, v in fields.items()}
        return await self._call("PATCH", f"/open/v1/digital-employees/{uid}", json=body)

    async def set_digital_employee_enabled(self, uid: UUID, enabled: bool) -> dict[str, Any]:
        action = "enable" if enabled else "disable"
        return await self._call("POST", f"/open/v1/digital-employees/{uid}/{action}")

    # ───────────────────────────── 数据权限（AtlasAgent 等，权限设计 §7.2）

    async def data_check(self, data_code: str, data_id: str, user_id: UUID) -> str:
        """用户对某条数据的有效级别（含编码级授权）→ NONE | READ | WRITE | OWNER"""
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": data_code, "data_id": data_id, "user_id": str(user_id)},
        )
        return str(r["permission"])

    async def data_accessible(
        self, data_code: str, user_id: UUID, *, min_level: str = "READ"
    ) -> tuple[dict[str, str], str]:
        """→ ({data_id: 级别}, 编码级授权给的级别)。翻页取全。"""
        out: dict[str, str] = {}
        page, type_level = 1, "NONE"
        while True:
            r = await self._call(
                "GET",
                "/open/v1/data-permissions/accessible",
                params={
                    "data_code": data_code,
                    "user_id": str(user_id),
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

    async def data_grant(
        self,
        data_code: str,
        data_id: str,
        grants: list[dict[str, Any]],
        *,
        data_name: str | None = None,
        operator_id: UUID | None = None,
    ) -> Any:
        """批量写授权：首写（数据还没登记）必须含一名 OWNER；之后要 operator_id 是 Owner。

        grants：[{permission, subject_type: USER|DEPT, subject_id, include_sub}]
        """
        body: dict[str, Any] = {"data_code": data_code, "data_id": data_id, "grants": grants}
        if data_name is not None:
            body["data_name"] = data_name
        if operator_id is not None:
            body["operator_id"] = str(operator_id)
        return await self._call("POST", "/open/v1/data-permissions/batch", json=body)

    async def data_acl(self, data_code: str, data_id: str) -> list[dict[str, Any]]:
        """这条数据的授权明细；还没登记 → []"""
        try:
            return list(
                await self._call(
                    "GET",
                    "/open/v1/data-permissions/acl",
                    params={"data_code": data_code, "data_id": data_id},
                )
            )
        except UCError as exc:
            if exc.status_code == 404:
                return []
            raise

    async def data_acl_update(
        self, acl_id: str, operator_id: UUID, *, level: str | None, include_sub: bool | None
    ) -> None:
        body: dict[str, Any] = {"operator_id": str(operator_id)}
        if level is not None:
            body["permission"] = level
        if include_sub is not None:
            body["include_sub"] = include_sub
        await self._call("PATCH", f"/open/v1/data-permissions/acl/{acl_id}", json=body)

    async def data_acl_delete(self, acl_id: str, operator_id: UUID) -> None:
        await self._call(
            "DELETE",
            f"/open/v1/data-permissions/acl/{acl_id}",
            params={"operator_id": str(operator_id)},
        )

    async def data_rename(self, data_code: str, data_id: str, data_name: str) -> None:
        try:
            await self._call(
                "PATCH",
                "/open/v1/data-objects",
                json={"data_code": data_code, "data_id": data_id, "data_name": data_name},
            )
        except UCError as exc:
            if exc.status_code != 404:  # 还没登记：无事可做
                raise
