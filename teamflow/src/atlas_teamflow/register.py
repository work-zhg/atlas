"""把 TeamFlow 的操作、菜单、角色、数据编码登记到用户中心（幂等）。

走用户中心管理 API（需要一个有 app:manage + role:manage 的管理员账号）：
- 应用不存在则创建并输出 App Key / Secret（Secret 只输出这一次）；
- 操作、菜单按目录补齐，目录里没有的删除（角色里自定义的授权会随之失效，先 dry-run 列出）；
- 三个内置角色按目录覆盖其操作与菜单；其它自建角色不动；
- 数据编码 Team（数据管理员操作码 team:manage_all）。
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import httpx

from . import catalog


class _UC:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=15.0)
        self.csrf = ""

    def login(self, account: str, password: str) -> None:
        r = self.http.post("/api/v1/auth/login", json={"account": account, "password": password})
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

    def send(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        return self._check(
            self.http.request(
                method, path, json=body, params=params, headers={"X-UC-CSRF": self.csrf}
            )
        )


def register(
    base_url: str, account: str, password: str, *, rotate: bool = False
) -> dict[str, str | None]:
    uc = _UC(base_url)
    uc.login(account, password)
    out: dict[str, str | None] = {"app_key": None, "app_secret": None}

    app = next((a for a in uc.get("/api/v1/apps") if a["name"] == catalog.APP_NAME), None)
    if app is None:
        r = uc.send(
            "POST",
            "/api/v1/apps",
            {
                "name": catalog.APP_NAME,
                "description": "AI Agent 作为团队成员参与研发流程",
                "icon": "🧩",
            },
        )
        app, out["app_secret"] = r["app"], r["app_secret"]
        print(f"✓ 已创建应用「{catalog.APP_NAME}」")
    elif rotate:
        out["app_secret"] = uc.send("POST", f"/api/v1/apps/{app['id']}/rotate-secret")["app_secret"]
        print("✓ 已轮换 App Secret")
    out["app_key"] = app["app_key"]
    aid = app["id"]
    uc.send("PUT", f"/api/v1/apps/{aid}/scope", {"all": True, "dept_ids": []})

    # ── 操作
    ops = {o["code"]: o for o in uc.get(f"/api/v1/apps/{aid}/operations")}
    want = {code for _, code, _ in catalog.OPERATIONS}
    for module, code, name in catalog.OPERATIONS:
        if code not in ops:
            ops[code] = uc.send(
                "POST",
                f"/api/v1/apps/{aid}/operations",
                {"module": module, "code": code, "name": name},
            )
        elif (ops[code]["module"], ops[code]["name"]) != (module, name):
            uc.send(
                "PATCH", f"/api/v1/operations/{ops[code]['id']}", {"module": module, "name": name}
            )
    for code, o in list(ops.items()):
        if code not in want:
            uc.send("DELETE", f"/api/v1/operations/{o['id']}")
            print(f"  - 删除目录外的操作 {code}")
            ops.pop(code)

    # ── 菜单（先删目录外的子菜单，再删目录）
    menus = {m["code"]: m for m in uc.get(f"/api/v1/apps/{aid}/menus")}
    want_menus = {m["code"] for m in catalog.MENUS}
    for m in sorted(
        (m for c, m in menus.items() if c not in want_menus), key=lambda m: m["type"] == "dir"
    ):
        uc.send("DELETE", f"/api/v1/menus/{m['id']}")
        print(f"  - 删除目录外的菜单 {m['code']}")
        menus.pop(m["code"])
    for spec in catalog.MENUS:
        parent_id = menus[spec["parent"]]["id"] if spec.get("parent") else None
        body = {
            "name": spec["name"],
            "icon": spec.get("icon"),
            "path": spec.get("path"),
            "is_public": spec.get("is_public", False),
            "parent_id": parent_id,
        }
        if spec["code"] not in menus:
            menus[spec["code"]] = uc.send(
                "POST",
                f"/api/v1/apps/{aid}/menus",
                {**body, "type": spec["type"], "code": spec["code"]},
            )
        else:
            uc.send("PATCH", f"/api/v1/menus/{menus[spec['code']]['id']}", body)
    _order_menus(uc, aid)

    # ── 角色
    roles = {r["code"]: r for r in uc.get(f"/api/v1/apps/{aid}/roles")}
    for spec in catalog.ROLES:
        if spec["code"] not in roles:
            roles[spec["code"]] = uc.send(
                "POST",
                f"/api/v1/apps/{aid}/roles",
                {"code": spec["code"], "name": spec["name"], "description": spec["description"]},
            )
        else:
            uc.send(
                "PATCH",
                f"/api/v1/roles/{roles[spec['code']]['id']}",
                {"name": spec["name"], "description": spec["description"]},
            )
        rid = roles[spec["code"]]["id"]
        uc.send(
            "PUT",
            f"/api/v1/roles/{rid}/operations",
            {"ids": [ops[c]["id"] for c in spec["operations"]]},
        )
        uc.send(
            "PUT", f"/api/v1/roles/{rid}/menus", {"ids": [menus[c]["id"] for c in spec["menus"]]}
        )

    # ── 数据编码
    types = {t["code"]: t for t in uc.get(f"/api/v1/apps/{aid}/data-types")}
    dt = catalog.DATA_TYPE
    if dt["code"] not in types:
        uc.send("POST", f"/api/v1/apps/{aid}/data-types", dt)
    else:
        uc.send(
            "PATCH",
            f"/api/v1/data-types/{types[dt['code']]['id']}",
            {k: dt[k] for k in ("name", "description", "admin_operation_code")},
        )
    print(
        f"✓ 已同步：{len(catalog.OPERATIONS)} 个操作、{len(catalog.MENUS)} 个菜单、"
        f"{len(catalog.ROLES)} 个角色、数据编码 {dt['code']}"
    )
    return out


def _order_menus(uc: _UC, aid: str) -> None:
    """同级菜单按目录顺序排列。用户中心只提供「上移」，逐个上移到位（菜单很少，次数可忽略）。"""
    want = [m["code"] for m in catalog.MENUS]
    for _ in range(len(want) ** 2):
        rows = sorted(uc.get(f"/api/v1/apps/{aid}/menus"), key=lambda m: m["sort"])
        siblings: dict[str | None, list[dict[str, Any]]] = {}
        for m in rows:
            siblings.setdefault(m["parent_id"], []).append(m)
        moved = False
        for group in siblings.values():
            for prev, cur in pairwise(group):
                if want.index(cur["code"]) < want.index(prev["code"]):
                    uc.send("POST", f"/api/v1/menus/{cur['id']}/move-up")
                    moved = True
                    break
            if moved:
                break
        if not moved:
            return
