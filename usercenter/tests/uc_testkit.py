"""测试工具：带 Cookie 与 CSRF 头的客户端、常用构造。"""

from __future__ import annotations

import os
from typing import Any

import httpx
from atlas_usercenter.api.app import create_app

STRONG = "Passw0rd!"


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

    async def login(self, account: str, password: str) -> httpx.Response:
        return await self.post("/api/v1/auth/login", {"account": account, "password": password})

    async def login_and_activate(self, account: str, temp: str, new: str = STRONG) -> None:
        r = await self.login(account, temp)
        assert r.status_code == 200, r.text
        if r.json()["must_change_password"]:
            r = await self.post(
                "/api/v1/auth/password", {"old_password": temp, "new_password": new}
            )
            assert r.status_code == 200, r.text


async def admin_client() -> Client:
    c = Client()
    await c.login_and_activate("admin", os.environ["UC_TEST_ADMIN_TEMP"])
    return c


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
