"""模型网关测试的构造工具（不叫 helpers / testkit：避免与别处同名模块撞车）。"""

from __future__ import annotations

import itertools
from typing import Any

from atlas_modelgateway.errors import Conflict, Forbidden, Unavailable
from atlas_modelgateway.perms import LEVEL_RANK, Grant, SubjectModels

#: 测试里的用户（用户中心的 uid）
ADMIN = "00000000-0000-7000-8000-00000000000a"
ALICE = "00000000-0000-7000-8000-0000000000a1"
BOB = "00000000-0000-7000-8000-0000000000b0"
DIGITAL = "00000000-0000-7000-8000-0000000000de"
GONE = "00000000-0000-7000-8000-0000000000ff"
DEPT = "00000000-0000-7000-8000-0000000000d1"


def as_user(uid: str) -> dict[str, str]:
    return {"X-Uid": uid}


class FakePermissions:
    """用户中心的内存替身：模型名 → {用户: 级别}；只有直接授给用户的。"""

    def __init__(self) -> None:
        self.grants: dict[str, dict[str, str]] = {}
        #: 「全部模型」这类编码级授权：用户 → 级别
        self.type_levels: dict[str, str] = {ADMIN: "OWNER"}
        self.users: dict[str, dict[str, Any]] = {
            ADMIN: {
                "id": ADMIN,
                "name": "管理员",
                "user_type": "human",
                "status": "active",
                "dept": {"id": DEPT},
            },
            ALICE: {
                "id": ALICE,
                "name": "alice",
                "user_type": "human",
                "status": "active",
                "dept": {"id": DEPT},
            },
            BOB: {"id": BOB, "name": "bob", "user_type": "human", "status": "active", "dept": None},
            DIGITAL: {
                "id": DIGITAL,
                "name": "工单助手",
                "user_type": "digital",
                "status": "active",
                "dept": None,
            },
            GONE: {
                "id": GONE,
                "name": "离职的人",
                "user_type": "human",
                "status": "disabled",
                "dept": None,
            },
        }
        self.registered: list[str] = []
        self.down = False
        self.calls = 0
        self._acl_ids = itertools.count(1)
        self.acl_rows: dict[str, tuple[str, str]] = {}

    def _check(self) -> None:
        self.calls += 1
        if self.down:
            raise Unavailable("用户中心暂不可用", code="AUTHZ_UNAVAILABLE")

    def _level(self, user_id: str, model: str) -> str:
        own = self.grants.get(model, {}).get(user_id, "NONE")
        typ = self.type_levels.get(user_id, "NONE")
        return own if LEVEL_RANK[own] >= LEVEL_RANK[typ] else typ

    def _accessible(self, user_id: str, min_level: str) -> SubjectModels:
        names = {
            m for m in self.grants if LEVEL_RANK[self._level(user_id, m)] >= LEVEL_RANK[min_level]
        }
        typ = self.type_levels.get(user_id, "NONE")
        return SubjectModels(frozenset(names), LEVEL_RANK[typ] >= LEVEL_RANK[min_level])

    async def usable(self, subject: str) -> SubjectModels:
        self._check()
        return self._accessible(subject, "READ")

    async def owned(self, user_id: str) -> SubjectModels:
        self._check()
        return self._accessible(user_id, "OWNER")

    async def level(self, user_id: str, model: str) -> str:
        self._check()
        return self._level(user_id, model)

    async def register(self, model: str, display_name: str, owner: str) -> None:
        self._check()
        if self.grants.get(model):
            raise Conflict(f"用户中心里已经登记过模型 {model}", code="UC_REGISTERED")
        self.grants[model] = {owner: "OWNER"}
        self.registered.append(model)

    async def rename(self, model: str, display_name: str) -> None:
        self._check()

    async def acl(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {"id": acl_id, "subject_id": uid, "permission": self.grants[m][uid]}
            for acl_id, (m, uid) in self.acl_rows.items()
            if m == model and uid in self.grants.get(m, {})
        ]

    async def grant(self, model: str, operator: str, grants: list[Grant]) -> None:
        self._check()
        if self._level(operator, model) != "OWNER":
            raise Forbidden("只有所有者可以授权", code="NOT_DATA_OWNER")
        for g in grants:
            self.grants.setdefault(model, {})[g.subject_id] = g.level
            self.acl_rows[str(next(self._acl_ids))] = (model, g.subject_id)

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None:
        self._check()
        model, uid = self.acl_rows[acl_id]
        if level is not None:
            self.grants[model][uid] = level

    async def revoke(self, acl_id: str, operator: str) -> None:
        self._check()
        model, uid = self.acl_rows.pop(acl_id)
        if self._level(operator, model) != "OWNER":
            raise Forbidden("只有所有者可以收回授权", code="NOT_DATA_OWNER")
        self.grants[model].pop(uid, None)

    async def holders(self, model: str) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "user": {"id": uid, "name": self.users.get(uid, {}).get("name", uid)},
                "permission": lv,
            }
            for uid, lv in self.grants.get(model, {}).items()
        ]

    async def user(self, user_id: str) -> dict[str, Any] | None:
        self._check()
        return self.users.get(user_id)

    def allow(self, model: str, *users: str) -> None:
        for uid in users:
            self.grants.setdefault(model, {})[uid] = "READ"


# ───────────────────────────── 场景

CHAT_CAPS = {"tools": True, "temperature": True, "stream_usage": True}


async def register(
    client: Any,
    name: str,
    *,
    kind: str = "chat",
    protocols: list[str] | None = None,
    upstream: str | None = None,
    activate: bool = True,
    **attrs: Any,
) -> Any:
    body: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "protocols": protocols
        or {"chat": ["anthropic", "openai"], "embedding": ["openai"], "rerank": ["rerank"]}[kind],
        "provider": "fake",
        **attrs,
    }
    if upstream:
        body["upstream_model"] = upstream
    if kind == "chat":
        body.setdefault("context_window", 128_000)
        body.setdefault("max_output_tokens", 8_192)
        body.setdefault("capabilities", CHAT_CAPS)
    r = await client.post("/api/v1/models", json=body, headers=as_user(ADMIN))
    if r.status_code != 201 or not activate:
        return r
    r2 = await client.post(f"/api/v1/models/{name}/activate", headers=as_user(ADMIN))
    assert r2.status_code == 200, r2.text
    return r2


async def key_of(client: Any, subject: str) -> str:
    """经内部接口取主体的 Key（没有就生成）。"""
    access = (await client.get(f"/internal/subjects/{subject}/access")).json()
    assert access["key"], access
    return str(access["key"])


def anthropic_body(model: str, *, stream: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "你好"}],
        **({"stream": True} if stream else {}),
        **extra,
    }


def openai_body(model: str, *, stream: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "你好"}],
        **({"stream": True} if stream else {}),
        **extra,
    }
