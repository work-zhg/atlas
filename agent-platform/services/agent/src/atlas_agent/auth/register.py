"""把 Atlas 的操作码、菜单、角色、数据编码登记到用户中心（幂等，权限设计 §6）。

走用户中心管理 API（需要一个有 app:manage + role:manage 的管理员账号）：
- 应用不存在则创建（应用编码 atlas：内网调用开放接口靠它认应用，不用 App Key / Secret）；
  可访问范围只在创建时设为全员，之后不覆盖（管理员在用户中心收窄后，再次同步不会改回去）；
- 操作、菜单按目录补齐，目录里没有的删除；
- 三个内置角色按目录覆盖其操作与菜单；其它自建角色不动；
- 数据编码 AtlasAgent、AtlasKnowledgeBase。「管理全部」是数据编码级授权，
  在用户中心授；退役的「管理全部」操作码（catalog.RETIRED_ADMIN_OPS）删除前，持有人换成
  「全部数据 · Owner」；
- MCP 交给 MCP 网关（catalog.RETIRED_MCP_*）：操作码 mcp:manage、数据编码 AtlasMcpServer 删除前，
  授权搬到网关的应用名下。要先登记网关（python -m atlas_mcpgateway register-uc）。

★ 与 AI TeamFlow 的同名脚本同一套做法，各自一份（不为几十行抽公共包）。
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

    def login(self, account: str, password: str, tenant: str | None = None) -> None:
        body = {"account": account, "password": password}
        if tenant:
            body["tenant"] = tenant  # 有多个租户时必填：登记要在平台租户里做
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

    def send(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        return self._check(
            self.http.request(
                method, path, json=body, params=params, headers={"X-UC-CSRF": self.csrf}
            )
        )

    def open_delete(self, data_code: str, data_id: str, me: dict[str, Any]) -> None:
        """以 Atlas 应用的身份经开放接口清掉一条数据的全部授权（内网免令牌：X-Caller-App）。"""
        headers = {
            "X-Caller-App": catalog.APP_CODE,
            "X-Uid": str(me["user"]["id"]),
            "X-Tenant-Id": str(me["tenant"]["id"]),
        }
        self._check(
            self.http.delete(
                "/open/v1/data-permissions",
                params={"data_code": data_code, "data_id": data_id},
                headers=headers,
            )
        )


def _retire_admin_ops(uc: _UC, aid: str, ops: dict[str, Any]) -> None:
    """退役的「管理全部」操作码：持有它的角色授权（直接 + 经岗位）→ 该编码的「全部数据 · Owner」。

    ★ 角色归角色、数据权限归数据权限：「管理全部知识库」是对数据的级别，不该经角色表达。
    ★ 幂等：用户中心按 (编码, 主体) 去重，重复执行只是更新为同样的级别。
    """
    for op_code, data_code in catalog.RETIRED_ADMIN_OPS.items():
        op = ops.get(op_code)
        if op is None:
            continue
        types = {t["code"]: t for t in uc.get(f"/api/v1/apps/{aid}/data-types")}
        dt = types.get(data_code)
        if dt is None:
            spec = next(d for d in catalog.DATA_TYPES if d["code"] == data_code)
            dt = uc.send("POST", f"/api/v1/apps/{aid}/data-types", spec)
        moved = 0
        for role in uc.get(f"/api/v1/apps/{aid}/roles"):
            detail = uc.get(f"/api/v1/roles/{role['id']}")
            if op["id"] not in detail.get("operation_ids", []):
                continue
            grants = uc.get(f"/api/v1/roles/{role['id']}/grants")
            rows = list(grants["grants"]) + [g for p in grants["positions"] for g in p["grants"]]
            for row in rows:
                subject = row["subject"]
                uc.send(
                    "POST",
                    f"/api/v1/data-types/{dt['id']}/acl",
                    {
                        "level": "OWNER",
                        "subjects": [{"type": subject["type"], "id": subject["id"]}],
                        "include_sub": bool(row.get("include_sub")),
                    },
                )
                moved += 1
        print(f"  ↪ {op_code} 退役：{moved} 条授权换成「{dt['name']} · 全部 · Owner」")


def _retire_mcp(uc: _UC, aid: str, ops: dict[str, Any]) -> set[str]:
    """MCP 交给 MCP 网关（doc/mcp-design.html 第二版 §5.3）。→ 这次还不能删的操作码。

    ① 操作码 mcp:manage 的持有人（直接 + 经岗位）→ 网关的角色 MCP_ADMIN +「全部 MCP 服务 · 所有者」
       （MCP 管理员要能给别人授权：角色管「能不能做」，「对哪些服务」是数据权限）
    ② 数据编码 AtlasMcpServer 的授权 → McpServer：编码级的按原范围搬；单条的按数据标识（服务名）
       对上网关里登记过的服务。全部搬完才清掉旧数据、删旧编码；对不上的先保留并列出
       （先在网关登记这些服务：python -m atlas_mcpgateway import …，再跑一次）
    ★ 网关还没登记到用户中心时什么都不删：删了授权就丢了。
    ★ 幂等：用户中心按 (编码 / 角色, 主体) 去重，重复执行只是更新为同样的级别。
    """
    old_op = ops.get(catalog.RETIRED_MCP_OPERATION)
    old_types = {t["code"]: t for t in uc.get(f"/api/v1/apps/{aid}/data-types")}
    old_dt = old_types.get(catalog.RETIRED_MCP_DATA)
    if old_op is None and old_dt is None:
        return set()
    gw = next((a for a in uc.get("/api/v1/apps") if a.get("code") == catalog.MCP_GATEWAY_APP), None)
    gw_role = gw_dt = None
    if gw is not None:
        gw_role = next(
            (
                r
                for r in uc.get(f"/api/v1/apps/{gw['id']}/roles")
                if r["code"] == catalog.MCP_GATEWAY_ADMIN_ROLE
            ),
            None,
        )
        gw_dt = next(
            (
                t
                for t in uc.get(f"/api/v1/apps/{gw['id']}/data-types")
                if t["code"] == catalog.MCP_GATEWAY_DATA
            ),
            None,
        )
    if gw_role is None or gw_dt is None:
        print(
            "  ⚠ MCP 网关还没登记到用户中心：mcp:manage、AtlasMcpServer 先保留。"
            "先跑 python -m atlas_mcpgateway register-uc，再跑一次"
        )
        return {catalog.RETIRED_MCP_OPERATION}

    if old_op is not None:
        moved = 0
        for role in uc.get(f"/api/v1/apps/{aid}/roles"):
            if old_op["id"] not in uc.get(f"/api/v1/roles/{role['id']}").get("operation_ids", []):
                continue
            grants = uc.get(f"/api/v1/roles/{role['id']}/grants")
            rows = list(grants["grants"]) + [g for p in grants["positions"] for g in p["grants"]]
            for row in rows:
                subject = {"type": row["subject"]["type"], "id": row["subject"]["id"]}
                include_sub = bool(row.get("include_sub"))
                uc.send(
                    "POST",
                    "/api/v1/grants",
                    {
                        "kind": "role",
                        "target_ids": [gw_role["id"]],
                        "subjects": [subject],
                        "include_sub": include_sub,
                    },
                )
                uc.send(
                    "POST",
                    f"/api/v1/data-types/{gw_dt['id']}/acl",
                    {"level": "OWNER", "subjects": [subject], "include_sub": include_sub},
                )
                moved += 1
        print(
            f"  ↪ mcp:manage 退役：{moved} 条授权换成 MCP 网关的 MCP 管理员"
            " +「全部 MCP 服务 · 所有者」"
        )

    if old_dt is not None and not _move_mcp_data(uc, old_dt, gw_dt):
        return set()
    if old_dt is not None:
        uc.send("DELETE", f"/api/v1/data-types/{old_dt['id']}")
        print(f"  - 删除数据编码 {catalog.RETIRED_MCP_DATA}（授权已搬到 MCP 网关的 McpServer）")
    return set()


def _data_objects(uc: _UC, code: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        r = uc.get("/api/v1/data", scope="all", data_code=code, page=page, size=200)
        out.extend(r.get("items") or [])
        if len(out) >= int(r.get("total") or 0) or not r.get("items"):
            return out
        page += 1


def _move_mcp_data(uc: _UC, old_dt: dict[str, Any], gw_dt: dict[str, Any]) -> bool:
    """AtlasMcpServer 的授权 → McpServer。→ 旧编码现在能不能删（单条数据都对上、清掉了）。"""
    for row in uc.get(f"/api/v1/data-types/{old_dt['id']}/acl").get("acl") or []:
        uc.send(
            "POST",
            f"/api/v1/data-types/{gw_dt['id']}/acl",
            {
                "level": row["permission"],
                "subjects": [{"type": row["subject"]["type"].lower(), "id": row["subject"]["id"]}],
                "include_sub": bool(row.get("include_sub")),
            },
        )
    objects = _data_objects(uc, catalog.RETIRED_MCP_DATA)
    if not objects:
        return True
    targets = {o["data_id"]: o for o in _data_objects(uc, catalog.MCP_GATEWAY_DATA)}
    missing = sorted(o["data_id"] for o in objects if o["data_id"] not in targets)
    if missing:
        print(
            f"  ⚠ {catalog.RETIRED_MCP_DATA} 先保留：MCP 网关里还没有 {'、'.join(missing)}。"
            "先在网关登记这些服务（python -m atlas_mcpgateway import …），再跑一次"
        )
        return False
    me = uc.get("/api/v1/auth/me")
    for obj in objects:
        for a in uc.get(f"/api/v1/data/{obj['id']}").get("acl") or []:
            uc.send(
                "POST",
                f"/api/v1/data/{targets[obj['data_id']]['id']}/acl",
                {
                    "level": a["level"],
                    "subjects": [{"type": a["subject"]["type"], "id": a["subject"]["id"]}],
                    "include_sub": bool(a.get("include_sub")),
                },
            )
        # 旧数据只能由所属应用（Atlas）经开放接口清理：数据编码下还有数据时用户中心不许删编码
        uc.open_delete(catalog.RETIRED_MCP_DATA, obj["data_id"], me)
    print(f"  ↪ {catalog.RETIRED_MCP_DATA}：{len(objects)} 条数据的授权搬到 McpServer")
    return True


def register(base_url: str, account: str, password: str, *, tenant: str | None = None) -> None:
    uc = _UC(base_url)
    uc.login(account, password, tenant)

    app = next((a for a in uc.get("/api/v1/apps") if a["name"] == catalog.APP_NAME), None)
    if app is None:
        r = uc.send(
            "POST",
            "/api/v1/apps",
            {
                "name": catalog.APP_NAME,
                "code": catalog.APP_CODE,
                "description": catalog.APP_DESCRIPTION,
                "icon": catalog.APP_ICON,
            },
        )
        app = r["app"]
        uc.send("PUT", f"/api/v1/apps/{app['id']}/scope", {"all": True, "dept_ids": []})
        print(f"✓ 已创建应用「{catalog.APP_NAME}」（可访问范围：全员，可在用户中心收窄）")
    elif app.get("code") != catalog.APP_CODE:
        # 内网免令牌：用户中心按 X-Caller-App 认应用（内网身份传递 §3）
        uc.send("PATCH", f"/api/v1/apps/{app['id']}", {"code": catalog.APP_CODE})
        print(f"✓ 应用编码设为 {catalog.APP_CODE}")
    aid = app["id"]

    # ── 操作
    ops = {o["code"]: o for o in uc.get(f"/api/v1/apps/{aid}/operations")}
    _retire_admin_ops(uc, aid, ops)  # 必须在删除目录外的操作码之前
    keep = _retire_mcp(uc, aid, ops)  # 同上；网关还没登记时 mcp:manage 这次先不删
    want = {code for _, code, _ in catalog.OPERATIONS} | keep
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

    # ── 菜单：先建 / 改（含挪到新目录），再删目录外的（先子菜单后目录）
    #    ★ 顺序不能反：菜单挪走之前旧目录下还有子菜单，用户中心不允许删
    menus = {m["code"]: m for m in uc.get(f"/api/v1/apps/{aid}/menus")}
    want_menus = {m["code"] for m in catalog.MENUS}
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
    for m in sorted(
        (m for c, m in menus.items() if c not in want_menus), key=lambda m: m["type"] == "dir"
    ):
        uc.send("DELETE", f"/api/v1/menus/{m['id']}")
        print(f"  - 删除目录外的菜单 {m['code']}")
        menus.pop(m["code"])
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
    for dt in catalog.DATA_TYPES:
        if dt["code"] not in types:
            uc.send("POST", f"/api/v1/apps/{aid}/data-types", dt)
        else:
            uc.send(
                "PATCH",
                f"/api/v1/data-types/{types[dt['code']]['id']}",
                {k: dt[k] for k in ("name", "description")},
            )
    print(
        f"✓ 已同步：{len(catalog.OPERATIONS)} 个操作、{len(catalog.MENUS)} 个菜单、"
        f"{len(catalog.ROLES)} 个角色、数据编码 "
        + "、".join(d["code"] for d in catalog.DATA_TYPES)
    )


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
