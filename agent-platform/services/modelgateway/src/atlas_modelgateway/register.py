"""把模型管理平台登记到用户中心（幂等）：应用、操作码、角色、数据编码（方案第 4 章）。

走用户中心管理 API（需要一个有 app:manage + role:manage 的管理员账号）：
- 应用不存在则创建（应用编码 model-gateway：内网调开放接口时 X-Caller-App 写它；
  沿用自研网关时期的编码，已有的授权不用迁移）；可访问范围只在创建时设为全员；
  已存在则把名称、描述改成「模型管理」；
- 操作码 model:manage（登记、修改、删除模型；私有模型授权；客户端 Key）与角色 MODEL_ADMIN；
- 数据编码 Model：一个模型一条数据，「使用」= 只读（能选进自己的用户 Key）。

★ 模型管理员还要在用户中心拿到「全部模型 · 所有者」（数据编码级授权）：能用、能分配全部模型。
  角色只管「能不能做这类事」，「对哪些模型」是数据权限（两个领域不混）。
★ 与 MCP 网关、Atlas 的同名脚本同一套做法，各自一份（不为几十行抽公共包）。
"""

from __future__ import annotations

from typing import Any

import httpx

from .auth import MODEL_MANAGE
from .perms import DATA_CODE

APP_NAME = "模型管理"
APP_DESCRIPTION = "登记模型、私有模型授权、API Key；模型调用经 LiteLLM"
APP_ICON = "🧠"
OPERATIONS = [
    ("模型", MODEL_MANAGE, "登记、修改、删除模型；私有模型授权；客户端 Key")
]
ROLES = [
    {
        "code": "MODEL_ADMIN",
        "name": "模型管理员",
        "description": "登记、修改、删除模型；私有模型授权；客户端 Key 的分配、重置、停用",
        "operations": [MODEL_MANAGE],
    }
]
DATA_TYPES = [
    {
        "code": DATA_CODE,
        "name": "模型",
        "description": "一个模型一条：只读 = 使用（私有模型能选进自己的用户 Key）；"
        "Owner = 所有者",
    }
]


class _UC:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=15.0, trust_env=False)
        self.csrf = ""

    def login(self, account: str, password: str, tenant: str | None = None) -> None:
        body = {"account": account, "password": password}
        if tenant:
            body["tenant"] = tenant
        r = self.http.post("/api/v1/auth/login", json=body)
        self._check(r)
        self.csrf = self.http.cookies.get("uc_csrf") or ""
        if r.json().get("must_change_password"):
            raise SystemExit("✗ 该账号须先在用户中心修改密码")

    def _check(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                msg = r.json().get("message")
            except ValueError:
                msg = r.text
            raise SystemExit(
                f"✗ 用户中心 {r.request.method} {r.request.url.path} → {r.status_code}：{msg}"
            )
        return r.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._check(self.http.get(path, params=params))

    def send(self, method: str, path: str, body: Any = None) -> Any:
        return self._check(
            self.http.request(method, path, json=body, headers={"X-UC-CSRF": self.csrf})
        )


def register(
    base_url: str,
    account: str,
    password: str,
    *,
    app_code: str,
    tenant: str | None = None,
) -> None:
    uc = _UC(base_url)
    uc.login(account, password, tenant)

    app = next((a for a in uc.get("/api/v1/apps") if a.get("code") == app_code), None)
    if app is None:
        r = uc.send(
            "POST",
            "/api/v1/apps",
            {"name": APP_NAME, "code": app_code, "description": APP_DESCRIPTION, "icon": APP_ICON},
        )
        app = r["app"]
        uc.send("PUT", f"/api/v1/apps/{app['id']}/scope", {"all": True, "dept_ids": []})
        print(f"✓ 已创建应用「{APP_NAME}」（应用编码 {app_code}）")
    elif app.get("name") != APP_NAME:
        uc.send(
            "PATCH", f"/api/v1/apps/{app['id']}", {"name": APP_NAME, "description": APP_DESCRIPTION}
        )
        print(f"✓ 应用改名为「{APP_NAME}」")
    aid = app["id"]

    ops = {o["code"]: o for o in uc.get(f"/api/v1/apps/{aid}/operations")}
    for module, code, name in OPERATIONS:
        if code not in ops:
            ops[code] = uc.send(
                "POST",
                f"/api/v1/apps/{aid}/operations",
                {"module": module, "code": code, "name": name},
            )

    roles = {r["code"]: r for r in uc.get(f"/api/v1/apps/{aid}/roles")}
    for spec in ROLES:
        if spec["code"] not in roles:
            roles[spec["code"]] = uc.send(
                "POST",
                f"/api/v1/apps/{aid}/roles",
                {"code": spec["code"], "name": spec["name"], "description": spec["description"]},
            )
        uc.send(
            "PUT",
            f"/api/v1/roles/{roles[spec['code']]['id']}/operations",
            {"ids": [ops[c]["id"] for c in spec["operations"]]},
        )

    types = {t["code"]: t for t in uc.get(f"/api/v1/apps/{aid}/data-types")}
    for dt in DATA_TYPES:
        if dt["code"] not in types:
            uc.send("POST", f"/api/v1/apps/{aid}/data-types", dt)
        else:
            uc.send(
                "PATCH",
                f"/api/v1/data-types/{types[dt['code']]['id']}",
                {k: dt[k] for k in ("name", "description")},
            )
    print(f"✓ 已同步：操作码 {MODEL_MANAGE}、角色 MODEL_ADMIN、数据编码 {DATA_CODE}")
