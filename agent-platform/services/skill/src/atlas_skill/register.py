"""把技能应用登记到用户中心（幂等）：应用、操作码、角色。

走用户中心管理 API（需要一个有 app:manage + role:manage 的管理员账号）：
- 应用「技能」不存在则创建（应用编码 skill：内网调开放接口时 X-Caller-App 写它）；
  可访问范围只在创建时设为全员（范围内的人能看技能目录，管理员在用户中心收窄后不覆盖）；
- 操作码 skill:upload、skill:review、audit:view 与角色 SKILL_AUTHOR、SKILL_ADMIN。

★ 要在 Atlas 的 register-uc 之前跑：这三个操作码原来登记在 Atlas 名下，Atlas 退役它们时把
  持有人搬到这里的角色（atlas_agent/auth/register.py::_retire_skill）；这里没登记时 Atlas 先保留。
★ 与 Atlas、MCP 网关、模型管理平台的同名脚本同一套做法，各自一份（不为几十行抽公共包）。
"""

from __future__ import annotations

from typing import Any

import httpx

from .auth import AUDIT_VIEW, SKILL_REVIEW, SKILL_UPLOAD

APP_NAME = "技能"
APP_DESCRIPTION = "技能包的上传、扫描、审查、发布与下架；智能体引用这里发布的技能"
APP_ICON = "🧩"
OPERATIONS = [
    ("技能", SKILL_UPLOAD, "上传技能"),
    ("技能", SKILL_REVIEW, "审核技能（审查、停用、下架、重新检查）"),
    ("系统", AUDIT_VIEW, "查看审计"),
]
#: Atlas 退役操作码时按这里的编码找角色（atlas_agent/auth/catalog.py::RETIRED_SKILL_OPS）
ROLES = [
    {
        "code": "SKILL_AUTHOR",
        "name": "技能作者",
        "description": "上传技能；含脚本或非内置来源的技能要经审核才发布",
        "operations": [SKILL_UPLOAD],
    },
    {
        "code": "SKILL_ADMIN",
        "name": "技能管理员",
        "description": "上传与审核技能；停用、下架、重新检查；查看审计",
        "operations": [SKILL_UPLOAD, SKILL_REVIEW, AUDIT_VIEW],
    },
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
    print(
        f"✓ 已同步：操作码 {'、'.join(c for _, c, _ in OPERATIONS)}，"
        f"角色 {'、'.join(r['code'] for r in ROLES)}"
    )
