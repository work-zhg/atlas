"""用户中心开放接口（/open/v1）的 HTTP 客户端。TeamFlow 和用户中心的唯一接触点。

访问控制（授权快照、团队级别）和团队（成员与级别的读写）各自包一层，业务代码只依赖那两层。

★ 内网免令牌：应用身份 = 请求头 X-Caller-App: teamflow（决定数据编码属于哪个应用）；X-Uid、
  X-Tenant-Id 透传本次调用的身份（决定租户），见 outbound.py。
★ 测试里用内存实现替换（tests/fakes.py），方法签名一致。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from .errors import Forbidden, Invalid, NotFound, TFError, Unauthorized, Upstream
from .outbound import outbound_headers

__all__ = ["MARKET_CODE", "TEAM_CODE", "UserCenter"]

#: 团队的数据编码：单条授权 = 某个团队的级别；编码级授权 = 「全部团队」
TEAM_CODE = "Team"
#: 流程市场的数据编码：只看编码级授权（系统租户里有读写的人能维护市场）
MARKET_CODE = "FlowMarket"

_PASS_THROUGH = {401: Unauthorized, 403: Forbidden, 404: NotFound, 409: TFError, 422: Invalid}


class UserCenter:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=10.0, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, path, headers=outbound_headers(), **kw)
        except httpx.HTTPError as exc:
            raise Upstream("UC_UNAVAILABLE", "用户中心暂不可用，请稍后重试") from exc
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                raise Upstream("UC_ERROR", f"用户中心返回 {r.status_code}") from None
            cls = _PASS_THROUGH.get(r.status_code, Upstream)
            err = cls(
                body.get("code", "UC_ERROR"),
                body.get("message", "用户中心返回错误"),
                **(body.get("details") or {}),
            )
            if r.status_code == 409:
                err.status_code = 409
            raise err
        return r.json()

    # ───────────────────────────── 人、租户、授权快照

    async def authz(self, uid: UUID) -> dict[str, Any]:
        """{user: {id, name, status, tenant_id…}, can_access, roles, permissions, menus}"""
        return dict(await self._call("GET", f"/open/v1/users/{uid}/authz"))

    async def user(self, uid: UUID) -> dict[str, Any]:
        return dict(await self._call("GET", f"/open/v1/users/{uid}"))

    async def search_users(self, q: str | None, *, size: int = 20) -> list[dict[str, Any]]:
        r = await self._call("GET", "/open/v1/users", params={"q": q or "", "size": size})
        return list(r["items"])

    async def tenants(self) -> list[dict[str, Any]]:
        """开通了 TeamFlow 的租户：[{id, code, name, status, is_system}]。"""
        return list(await self._call("GET", "/open/v1/tenants"))

    # ───────────────────────────── 数据权限

    async def data_level(self, code: str, data_id: UUID, uid: UUID) -> str:
        """单条数据的有效级别（已含编码级授权）。"""
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/check",
            params={"data_code": code, "data_id": str(data_id), "user_id": str(uid)},
        )
        return str(r["permission"])

    async def type_level(self, code: str, uid: UUID) -> str:
        """对某编码下全部数据的级别（编码级授权）。"""
        r = await self._call(
            "GET", "/open/v1/data-permissions/type", params={"data_code": code, "user_id": str(uid)}
        )
        return str(r["permission"])

    async def accessible(self, code: str, uid: UUID, min_level: str = "READ") -> dict[str, str]:
        """有单条授权的数据 → {data_id: 级别}（不含编码级授权）。"""
        r = await self._call(
            "GET",
            "/open/v1/data-permissions/accessible",
            params={
                "data_code": code,
                "user_id": str(uid),
                "min": min_level,
                "scope": "data",
                "size": 500,
            },
        )
        return {i["data_id"]: i["permission"] for i in r["items"]}

    async def write_grants(
        self,
        code: str,
        data_id: UUID,
        data_name: str,
        grants: list[dict[str, Any]],
        operator: UUID | None,
    ) -> dict[str, Any]:
        """grants: [{permission, subject_type: USER|DEPT, subject_id, include_sub}]。

        数据还没有任何授权时（新建）可以不带 operator，但要有一条 OWNER；之后必须带 operator，
        且他是 Owner —— 用户中心兜底。
        """
        body: dict[str, Any] = {
            "data_code": code,
            "data_id": str(data_id),
            "data_name": data_name,
            "grants": grants,
        }
        if operator:
            body["operator_id"] = str(operator)
        return dict(await self._call("POST", "/open/v1/data-permissions/batch", json=body))

    async def acl(self, code: str, data_id: UUID) -> list[dict[str, Any]]:
        return list(
            await self._call(
                "GET",
                "/open/v1/data-permissions/acl",
                params={"data_code": code, "data_id": str(data_id)},
            )
        )

    async def holders(
        self, code: str, data_id: UUID, min_level: str = "READ"
    ) -> list[dict[str, Any]]:
        """展开到人的持有人：[{user: {id, name, account}, permission}]。"""
        return list(
            await self._call(
                "GET",
                "/open/v1/data-permissions/holders",
                params={"data_code": code, "data_id": str(data_id), "min": min_level},
            )
        )

    async def update_grant(self, acl_id: str, operator: UUID, *, permission: str) -> None:
        await self._call(
            "PATCH",
            f"/open/v1/data-permissions/acl/{acl_id}",
            json={"operator_id": str(operator), "permission": permission},
        )

    async def delete_grant(self, acl_id: str, operator: UUID) -> None:
        await self._call(
            "DELETE",
            f"/open/v1/data-permissions/acl/{acl_id}",
            params={"operator_id": str(operator)},
        )

    async def delete_data(self, code: str, data_id: UUID) -> None:
        """删一条数据的全部授权（删租户时清团队的授权）。"""
        await self._call(
            "DELETE",
            "/open/v1/data-permissions",
            params={"data_code": code, "data_id": str(data_id)},
        )

    async def rename_data(self, code: str, data_id: UUID, name: str) -> None:
        await self._call(
            "PATCH",
            "/open/v1/data-objects",
            json={"data_code": code, "data_id": str(data_id), "data_name": name},
        )
