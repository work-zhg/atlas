"""测试工具：带 Cookie 与 CSRF 头的客户端、运营中心的客户端（签名）、常用构造。"""

from __future__ import annotations

import json as _json
import os
from typing import Any
from uuid import uuid4

import httpx
from atlas_opsapi import OperatorStamp, sign
from atlas_usercenter.api.app import create_app

STRONG = "Passw0rd!"
#: 测试用的运营密钥（conftest 写进 ATLAS_UC_OPS_API_KEY）
OPS_KEY = "uc-test-ops-key"


class Client:
    """模拟浏览器：Cookie 由 httpx 保存，写请求自动带双提交的 CSRF 头。"""

    def __init__(self) -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://uc"
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(extra or {})
        csrf = self.http.cookies.get("uc_csrf")
        if csrf:
            h["X-UC-CSRF"] = csrf
        return h

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.post(
            url, json=json, headers=self._headers(kw.pop("headers", None)), **kw
        )

    async def patch(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.patch(
            url, json=json, headers=self._headers(kw.pop("headers", None)), **kw
        )

    async def put(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.put(
            url, json=json, headers=self._headers(kw.pop("headers", None)), **kw
        )

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.delete(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def login(self, account: str, password: str, tenant: str | None = None) -> httpx.Response:
        body = {"account": account, "password": password}
        if tenant:
            body["tenant"] = tenant
        return await self.post("/api/v1/auth/login", body)

    async def login_and_activate(
        self, account: str, temp: str, new: str = STRONG, tenant: str | None = None
    ) -> None:
        r = await self.login(account, temp, tenant)
        assert r.status_code == 200, r.text
        if r.json()["must_change_password"]:
            r = await self.post(
                "/api/v1/auth/password", {"old_password": temp, "new_password": new}
            )
            assert r.status_code == 200, r.text


class OpsClient:
    """模拟运营中心：每个请求按运营密钥签名，带上运营人员（OperatorStamp）。"""

    def __init__(self, key: str = OPS_KEY, operator: str = "小周") -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://uc"
        )
        self.key = key.encode()
        self.stamp = OperatorStamp(operator_uuid=uuid4(), operator_name=operator)

    async def close(self) -> None:
        await self.http.aclose()

    async def request(
        self, method: str, url: str, json: Any = None, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        body = b"" if json is None else _json.dumps(json, default=str).encode()
        target = httpx.URL(url, params=params)
        headers = {
            **sign(self.key, method, target.raw_path.decode(), body),
            **self.stamp.to_headers(),
            "Content-Type": "application/json",
        }
        return await self.http.request(method, target, content=body, headers=headers)

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, params=kw.get("params"))

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.request("POST", url, json, kw.get("params"))

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.request("PATCH", url, json)

    async def put(self, url: str, json: Any = None) -> httpx.Response:
        return await self.request("PUT", url, json)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", url, params=kw.get("params"))

    async def tenant_id(self, code: str) -> str:
        rows = (await self.get("/ops/v1/tenants")).json()
        return next(t["id"] for t in rows if t["code"] == code)


async def admin_client() -> Client:
    c = Client()
    # bootstrap 建的第一个租户 default；多租户时登录必须带企业代码
    await c.login_and_activate("admin", os.environ["UC_TEST_ADMIN_TEMP"], tenant="default")
    return c


async def make_tenant(
    ops: OpsClient, code: str, *, max_users: int | None = None, apps: list[str] | None = None
) -> tuple[str, Client]:
    """运营中心开通租户 → (租户 id, 已激活的租户管理员客户端)。"""
    r = await ops.post(
        "/ops/v1/tenants",
        {
            "uuid": str(uuid4()),
            "code": code,
            "name": f"{code} 公司",
            "max_users": max_users,
            "apps": apps or [],
            "admin": {"account": "boss", "name": "老板", "email": f"boss@{code}.com"},
        },
    )
    assert r.status_code == 201, r.text
    admin = Client()
    await admin.login_and_activate("boss", r.json()["temp_password"], tenant=code)
    return r.json()["tenant"]["id"], admin


async def make_app(
    name: str, code: str | None = None, *, tenants: tuple[str, ...] = ("default",), **extra: Any
) -> dict[str, Any]:
    """运营中心接入一个应用，给这些租户开通 → {"app": …, "app_secret": …}。"""
    ops = OpsClient()
    try:
        r = await ops.post("/ops/v1/apps", {"name": name, "code": code, **extra})
        assert r.status_code == 201, r.text
        app_id = r.json()["app"]["id"]
        for t in tenants:
            tid = await ops.tenant_id(t)
            s = await ops.put(f"/ops/v1/tenants/{tid}/apps/{app_id}", {"enabled": True})
            assert s.status_code == 200, s.text
        return r.json()
    finally:
        await ops.close()


async def root_id(c: Client) -> str:
    tree = (await c.get("/api/v1/depts/tree")).json()
    return next(d["id"] for d in tree if d["parent_id"] is None)


async def make_dept(c: Client, parent: str, name: str) -> str:
    r = await c.post("/api/v1/depts", {"parent_id": parent, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_user(c: Client, account: str, dept: str, name: str | None = None) -> tuple[str, str]:
    r = await c.post(
        "/api/v1/users",
        {
            "name": name or account,
            "account": account,
            "email": f"{account}@xinghai.com",
            "dept_id": dept,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["user"]["id"], r.json()["temp_password"]


async def role_id(c: Client, code: str) -> str:
    apps = (await c.get("/api/v1/apps")).json()
    for a in apps:
        for r in (await c.get(f"/api/v1/apps/{a['id']}/roles")).json():
            if r["code"] == code:
                return r["id"]
    raise AssertionError(code)
