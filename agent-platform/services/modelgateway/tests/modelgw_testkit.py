"""模型管理平台测试的替身（不叫 helpers / testkit：避免与别处同名模块撞车）。

FakePermissions   用户中心：模型名 → 授权（用户或部门）；编码级授权；用户
FakeLiteLLM       LiteLLM 管理接口：模型部署、Key（经 httpx.MockTransport，进程内）
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
from atlas_modelgateway.errors import Unavailable
from atlas_modelgateway.perms import LEVEL_RANK, Grant, SubjectModels

#: 测试里的用户（用户中心的 uid）
ADMIN = "00000000-0000-7000-8000-00000000000a"
ALICE = "00000000-0000-7000-8000-0000000000a1"
BOB = "00000000-0000-7000-8000-0000000000b0"
GONE = "00000000-0000-7000-8000-0000000000ff"
DEPT = "00000000-0000-7000-8000-0000000000d1"

MASTER_KEY = "test-master-key"


def as_user(uid: str) -> dict[str, str]:
    return {"X-Uid": uid}


def as_app(code: str = "atlas") -> dict[str, str]:
    return {"X-Caller-App": code}


class FakePermissions:
    """用户中心的内存替身。ALICE 在部门 DEPT；授给部门的授权覆盖部门里的人（不考虑上下级）。"""

    def __init__(self) -> None:
        #: 模型名 → [(acl id, 级别, 主体类型, 主体 id)]
        self.acls: dict[str, list[tuple[str, str, str, str]]] = {}
        #: 「全部模型」这类编码级授权：用户 → 级别
        self.type_levels: dict[str, str] = {ADMIN: "OWNER"}
        self.users: dict[str, dict[str, Any]] = {
            ADMIN: {"id": ADMIN, "name": "管理员", "status": "active", "dept": DEPT},
            ALICE: {"id": ALICE, "name": "alice", "status": "active", "dept": DEPT},
            BOB: {"id": BOB, "name": "bob", "status": "active", "dept": None},
            GONE: {"id": GONE, "name": "离职的人", "status": "disabled", "dept": None},
        }
        self.down = False
        self.removed: list[str] = []
        self._ids = itertools.count(1)

    def _check(self) -> None:
        if self.down:
            raise Unavailable("用户中心暂不可用", code="AUTHZ_UNAVAILABLE")

    def _covers(self, subject_type: str, subject_id: str, uid: str) -> bool:
        if subject_type == "user":
            return subject_id == uid
        return (self.users.get(uid) or {}).get("dept") == subject_id

    async def usable(self, subject: str) -> SubjectModels:
        self._check()
        names = {
            model
            for model, rows in self.acls.items()
            if any(self._covers(t, sid, subject) for _, _, t, sid in rows)
        }
        typ = self.type_levels.get(subject, "NONE")
        return SubjectModels(frozenset(names), LEVEL_RANK[typ] >= LEVEL_RANK["READ"])

    async def register(self, model: str, display_name: str, owner: str) -> None:
        self._check()
        if model not in self.acls:
            self.acls[model] = [(str(next(self._ids)), "OWNER", "user", owner)]

    async def rename(self, model: str, display_name: str) -> None:
        self._check()

    async def remove(self, model: str) -> None:
        self._check()
        self.acls.pop(model, None)
        self.removed.append(model)

    async def acl(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "id": acl_id,
                "permission": level,
                "subject": {"type": t.upper(), "id": sid, "name": sid},
                "include_sub": False,
            }
            for acl_id, level, t, sid in self.acls.get(model, [])
        ]

    async def grant(self, model: str, operator: str, grants: list[Grant]) -> None:
        self._check()
        rows = self.acls.setdefault(model, [])
        for g in grants:
            rows.append((str(next(self._ids)), g.level, g.subject_type, g.subject_id))

    async def revoke(self, acl_id: str, operator: str) -> None:
        self._check()
        for model, rows in self.acls.items():
            self.acls[model] = [r for r in rows if r[0] != acl_id]

    async def holders(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {"user": {"id": uid}, "permission": "READ"}
            for uid in self.users
            if any(self._covers(t, sid, uid) for _, _, t, sid in self.acls.get(model, []))
        ]

    async def user(self, user_id: str) -> dict[str, Any] | None:
        self._check()
        return self.users.get(user_id)

    async def user_by_account(self, account: str) -> dict[str, Any] | None:
        self._check()
        return next((u for u in self.users.values() if u["name"] == account), None)


class FakeLiteLLM:
    """LiteLLM 管理接口的内存替身：检查 master key，记下模型部署和 Key。"""

    def __init__(self) -> None:
        #: 部署 id → {model_name, litellm_params, model_info}
        self.models: dict[str, dict[str, Any]] = {}
        #: 原文 → {token, models, blocked, key_alias, metadata, user_id}
        self.keys: dict[str, dict[str, Any]] = {}
        self.down = False
        self.calls: list[tuple[str, str, Any]] = []
        self._n = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ── 测试里直接造的状态

    def add_config_model(self, name: str, provider: str = "dashscope", **info: Any) -> str:
        """LiteLLM 配置文件里的模型（平台没登记）。"""
        model_id = f"cfg-{next(self._n)}"
        self.models[model_id] = {
            "model_name": name,
            "litellm_params": {"model": f"{provider}/{name}", "custom_llm_provider": provider},
            "model_info": {"id": model_id, "db_model": False, **info},
        }
        return model_id

    def drop_model_named(self, name: str) -> None:
        """有人在 LiteLLM 界面里删了模型。"""
        self.models = {k: v for k, v in self.models.items() if v["model_name"] != name}

    def key_of(self, secret: str | None) -> dict[str, Any]:
        assert secret is not None and secret in self.keys, "LiteLLM 里没有这把 Key"
        return self.keys[secret]

    # ── 接口

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("litellm down", request=request)
        if request.headers.get("authorization") != f"Bearer {MASTER_KEY}":
            return httpx.Response(401, json={"error": {"message": "Authentication Error"}})
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        parts = path.strip("/").split("/")
        if request.method == "PATCH" and len(parts) == 3 and parts[::2] == ["model", "update"]:
            return self._model_patch(parts[1], body)
        handler = getattr(self, "_" + path.strip("/").replace("/", "_"), None)
        if handler is None:
            return httpx.Response(404, json={"detail": f"no route {path}"})
        return handler(request, body)  # type: ignore[no-any-return]

    def _model_info(self, request: httpx.Request, body: Any) -> httpx.Response:
        """同 LiteLLM：凭据不出现；litellm_params 里的自定义价格合并进 model_info。"""
        data = []
        for m in self.models.values():
            params = {k: v for k, v in m["litellm_params"].items() if k != "api_key"}
            prices = {k: v for k, v in params.items() if k.endswith("_cost_per_token")}
            data.append(
                {**m, "litellm_params": params, "model_info": {**m["model_info"], **prices}}
            )
        return httpx.Response(200, json={"data": data})

    def _model_new(self, request: httpx.Request, body: Any) -> httpx.Response:
        model_id = body["model_info"]["id"]
        if model_id in self.models:
            return httpx.Response(400, json={"error": {"message": "model id exists"}})
        self.models[model_id] = {
            "model_name": body["model_name"],
            "litellm_params": dict(body["litellm_params"]),
            "model_info": {**body["model_info"], "db_model": True},
        }
        return httpx.Response(200, json={"model_id": model_id})

    def _model_patch(self, model_id: str, body: Any) -> httpx.Response:
        """PATCH /model/{id}/update：按字段合并（LiteLLM 1.106 实测）。"""
        if model_id not in self.models:
            return httpx.Response(404, json={"detail": "model not found"})
        m = self.models[model_id]
        m["litellm_params"].update(body.get("litellm_params") or {})
        m["model_info"].update(body.get("model_info") or {})
        return httpx.Response(200, json={"model_id": model_id})

    def _model_delete(self, request: httpx.Request, body: Any) -> httpx.Response:
        if self.models.pop(body["id"], None) is None:
            return httpx.Response(404, json={"detail": "model not found"})
        return httpx.Response(200, json={})

    def _health_test_connection(self, request: httpx.Request, body: Any) -> httpx.Response:
        if body["litellm_params"].get("api_key") == "bad":
            return httpx.Response(200, json={"status": "error", "message": "AuthenticationError"})
        return httpx.Response(200, json={"status": "success"})

    def _key_generate(self, request: httpx.Request, body: Any) -> httpx.Response:
        alias = body.get("key_alias")
        if alias and any(k["key_alias"] == alias for k in self.keys.values()):
            # LiteLLM 1.106：别名全局唯一
            return httpx.Response(
                400, json={"error": {"message": f"Key with alias '{alias}' already exists"}}
            )
        n = next(self._n)
        secret = f"sk-test-{n:04d}"
        self.keys[secret] = {
            "token": f"hash-{n}",
            "models": list(body["models"]),
            "blocked": False,
            "key_alias": body.get("key_alias"),
            "metadata": body.get("metadata"),
            "user_id": body.get("user_id"),
        }
        return httpx.Response(200, json={"key": secret, "token": f"hash-{n}"})

    def _key_update(self, request: httpx.Request, body: Any) -> httpx.Response:
        if body["key"] not in self.keys:
            return httpx.Response(404, json={"detail": "key not found"})
        assert body["models"], "不能给 LiteLLM 推空列表（= 全部模型）"
        self.keys[body["key"]]["models"] = list(body["models"])
        return httpx.Response(200, json={})

    def _key_block(self, request: httpx.Request, body: Any) -> httpx.Response:
        return self._set_blocked(body["key"], True)

    def _key_unblock(self, request: httpx.Request, body: Any) -> httpx.Response:
        return self._set_blocked(body["key"], False)

    def _set_blocked(self, key: str, blocked: bool) -> httpx.Response:
        if key not in self.keys:
            return httpx.Response(404, json={"detail": "key not found"})
        self.keys[key]["blocked"] = blocked
        return httpx.Response(200, json={})

    def _key_delete(self, request: httpx.Request, body: Any) -> httpx.Response:
        for key in body["keys"]:
            self.keys.pop(key, None)
        return httpx.Response(200, json={})

    def _key_info(self, request: httpx.Request, body: Any) -> httpx.Response:
        key = request.url.params.get("key", "")
        if key not in self.keys:
            return httpx.Response(404, json={"detail": "key not found"})
        info = self.keys[key]
        return httpx.Response(
            200,
            json={
                "key": info["token"],
                "info": {"models": info["models"], "blocked": info["blocked"]},
            },
        )
