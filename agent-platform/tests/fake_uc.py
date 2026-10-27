"""用户中心的替身（没有免鉴权模式之后，所有经接口的测试都接它）。

conftest 的自动夹具给每个 create_app() 接一个 FakeUC；测试请求带上 AS_USER（X-Uid）即可。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from atlas_agent.auth.catalog import ALL_OPERATIONS

#: 测试请求的身份（原 dev 模式的默认用户）
TEST_USER = "00000000-0000-0000-0000-000000000001"
AS_USER = {"X-Uid": TEST_USER}

#: 没登记过的用户的授权快照：什么都能做
_ANYONE = {"can_access": True, "roles": [], "permissions": sorted(ALL_OPERATIONS), "menus": []}

_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


class FakeUC:
    """用户中心开放接口的替身：内网免令牌，只认请求头（X-Caller-App 必须是 atlas）。

    覆盖 Atlas 用到的：用户资料与授权快照、数据编码级授权、单条数据权限（AtlasAgent 等，只认
    USER 主体）、数字员工（数字员工设计 §4）。

    ★ 没用 add() 登记过的用户 = 什么都能做（全部操作码、全部数据 Owner）：大多数测试不关心
      权限，带个 X-Uid 就行。测权限的用例用 add() 登记具体的人。
    """

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        #: 数据编码级授权：用户 id → {数据编码: 级别}
        self.data_levels: dict[str, dict[str, str]] = {}
        #: 单条数据的授权：(数据编码, data_id) → [{id, level, subject_type, subject_id}]
        self.acl: dict[tuple[str, str], list[dict[str, Any]]] = {}
        #: 数字员工：id → 资料
        self.digital: dict[str, dict[str, Any]] = {}
        #: 收到的身份请求头（断言透传）
        self.seen: list[dict[str, str | None]] = []
        self.root_dept = str(uuid.uuid4())

    def add(self, account: str, ops: list[str], *, can_access: bool = True) -> str:
        uid = str(uuid.uuid4())
        self.users[account] = {
            "id": uid,
            "account": account,
            "name": account.upper(),
            "email": f"{account}@x.com",
            "status": "active",
            "dept": {"id": str(uuid.uuid4()), "path": "研发部"},
        }
        self.snapshots[uid] = {
            "can_access": can_access,
            "roles": ["ATLAS_MEMBER"],
            "permissions": ops,
            "menus": [{"name": "对话", "path": "/chat"}],
        }
        return uid

    def type_level(self, uid: str, code: str) -> str:
        """数据编码级授权：登记过的人按 data_levels（缺省 NONE），没登记的 = OWNER。"""
        if uid not in self.snapshots:
            return "OWNER"
        return self.data_levels.get(uid, {}).get(code, "NONE")

    def level(self, code: str, data_id: str, uid: str) -> str:
        own = [
            a["level"]
            for a in self.acl.get((code, data_id), [])
            if a["subject_type"] == "USER" and a["subject_id"] == uid
        ]
        return max([self.type_level(uid, code), *own], key=_RANK.__getitem__)

    def share(self, code: str, data_id: str, uid: str, level: str, subject: str = "USER") -> None:
        self.acl.setdefault((code, data_id), []).append(
            {"id": str(uuid.uuid4()), "level": level, "subject_type": subject, "subject_id": uid}
        )

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers
        assert req.headers.get("x-caller-app") == "atlas"
        self.seen.append({k: req.headers.get(k) for k in ("x-uid", "x-auth-uid", "x-tenant-id")})
        path, q = req.url.path, req.url.params
        body = json.loads(req.content) if req.content else {}
        ok = httpx.Response
        if path == "/open/v1/data-permissions/type":
            level = self.type_level(q["user_id"], q["data_code"])
            return ok(200, json={"permission": level, "sources": []})
        if path == "/open/v1/data-permissions/check":
            return ok(
                200, json={"permission": self.level(q["data_code"], q["data_id"], q["user_id"])}
            )
        if path == "/open/v1/data-permissions/accessible":
            code, uid = q["data_code"], q["user_id"]
            items = [
                {"data_id": d, "permission": self.level(c, d, uid)}
                for (c, d) in self.acl
                if c == code and _RANK[self.level(c, d, uid)] >= _RANK[q.get("min", "READ")]
            ]
            typ = self.type_level(uid, code)
            return ok(200, json={"items": items, "total": len(items), "type_permission": typ})
        if path == "/open/v1/data-permissions/batch":
            key = (body["data_code"], body["data_id"])
            if key in self.acl:
                op = body.get("operator_id")
                if op is None or self.level(*key, op) != "OWNER":
                    return ok(403, json={"code": "OWNER_REQUIRED", "message": "需要 Owner"})
            elif not any(g["permission"] == "OWNER" for g in body["grants"]):
                return ok(422, json={"code": "OWNER_REQUIRED", "message": "首写要 OWNER"})
            for g in body["grants"]:
                self.share(*key, g["subject_id"], g["permission"], g["subject_type"])
            return ok(201, json={"added": len(body["grants"])})
        if path == "/open/v1/data-permissions/acl":
            rows = self.acl.get((q["data_code"], q["data_id"]), [])
            return ok(
                200,
                json=[
                    {
                        "id": a["id"],
                        "permission": a["level"],
                        "subject": {"type": a["subject_type"], "id": a["subject_id"]},
                        "include_sub": a["subject_type"] == "DEPT",
                    }
                    for a in rows
                ],
            )
        if path.startswith("/open/v1/data-permissions/acl/"):
            acl_id = path.rsplit("/", 1)[1]
            for rows in self.acl.values():
                for a in list(rows):
                    if a["id"] == acl_id:
                        if req.method == "DELETE":
                            rows.remove(a)
                        else:
                            a["level"] = body.get("permission") or a["level"]
            return ok(200, json={"ok": True})
        if path == "/open/v1/data-objects":
            return ok(200, json={"ok": True})
        if path.startswith("/open/v1/digital-employees"):
            return self._digital(req.method, path, body)
        if path == "/open/v1/depts/tree":
            return ok(200, json=[{"id": self.root_dept, "parent_id": None, "name": "总部"}])
        uid = path.split("/")[4]
        if path.endswith("/authz"):
            return ok(200, json=self.snapshots.get(uid) or _ANYONE)
        u = next((x for x in self.users.values() if x["id"] == uid), None)
        return ok(
            200, json=u or {"id": uid, "account": "tester", "name": "测试", "status": "active"}
        )

    def _digital(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        parts = path.split("/")  # ['', 'open', 'v1', 'digital-employees', id?, action?]
        if len(parts) == 4:
            for de in self.digital.values():
                if de["external_ref"] == body["external_ref"]:
                    return httpx.Response(200, json=de)
            de_id = str(uuid.uuid4())
            de = {
                "id": de_id,
                "user_type": "digital",
                "account": f"dw-{de_id[:10]}",
                "name": body["name"][:32],
                "owner_id": body["owner_id"],
                "external_ref": body["external_ref"],
                "status": "active",
            }
            self.digital[de_id] = de
            # 数字员工也是用户：授权快照、资料都有（没有部门）
            self.snapshots[de_id] = {
                "can_access": True,
                "roles": [],
                "permissions": [],
                "menus": [],
            }
            self.users[de["account"]] = {**de, "email": None, "dept": None}
            return httpx.Response(201, json=de)
        de = self.digital[parts[4]]
        if len(parts) == 6:
            de["status"] = "active" if parts[5] == "enable" else "disabled"
        elif method == "PATCH":
            de.update({k: v for k, v in body.items() if v is not None})
        return httpx.Response(200, json=de)
