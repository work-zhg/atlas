"""记忆服务（atlas-memory）的客户端（doc/memory-design.html §3）。

★ 只经 HTTP，不 import atlas_memory（import-linter 守着）。
★ 实例由「作用域编码 + 关联主键」定位，都是接口参数：关联主键由 Atlas 按会话确定（ScopeRef），
  Agent 和抽取模型都不能指定（§4.1）。
★ 调用用户一律是会话所属用户（as_user）：X-Uid = X-Auth-Uid = 它。不用本轮的出站身份 ——
  以 Agent 为权限的数字员工对使用者的记忆没有权限（§4.1）。租户照常透传。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from ..errors import CapabilityUnavailable
from ..outbound import caller_app, current_identity

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryClient",
    "MemoryEntry",
    "MemoryRejected",
    "MemoryTopic",
    "MemoryUnavailable",
    "ScopeRef",
    "make_memory_client",
]


class MemoryUnavailable(CapabilityUnavailable):
    """记忆服务（或它背后的用户中心）暂不可用。调用方把它当作「本会话暂时不用记忆」。"""


class MemoryRejected(Exception):
    """记忆服务按规则拒绝了请求（4xx）。code 是服务端给的机器可读错误码。"""

    def __init__(self, status: int, code: str, message: str, context: dict[str, Any]) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.context = context


@dataclass(frozen=True)
class ScopeRef:
    """Atlas 用到的一个作用域实例。

    name  Atlas 侧的名字（工具参数、版本表、来源说明里用）：user
    code  记忆服务里的作用域编码（配置项 memory_user_scope）
    key   关联主键 —— 按会话确定
    label 给模型和用户看的名字
    """

    name: str
    code: str
    key: dict[str, str]
    label: str

    @property
    def path(self) -> str:
        key = ",".join(f"{k}={v}" for k, v in self.key.items())
        return f"/v1/scopes/{quote(self.code, safe='')}/instances/{quote(key, safe='=,')}"


@dataclass(frozen=True)
class MemoryTopic:
    code: str
    description: str
    version: int
    entry_count: int = 0
    char_count: int = 0
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def resident(self) -> bool:
        """常驻标记记在扩展属性里（§4.2）。"""
        return self.attrs.get("resident") is True

    @classmethod
    def of(cls, raw: dict[str, Any]) -> MemoryTopic:
        return cls(
            code=str(raw["code"]),
            description=str(raw.get("description") or ""),
            version=int(raw.get("version") or 0),
            entry_count=int(raw.get("entry_count") or 0),
            char_count=int(raw.get("char_count") or 0),
            attrs=dict(raw.get("attrs") or {}),
        )


@dataclass(frozen=True)
class MemoryEntry:
    number: int
    content: str
    topic: str | None
    occurred_at: str
    source: str = ""
    origin: dict[str, Any] | None = None
    status: str = "active"
    superseded_by: int | None = None
    retired_at: str | None = None
    created_at: str | None = None

    @property
    def date(self) -> str:
        return self.occurred_at[:10]

    @classmethod
    def of(cls, raw: dict[str, Any]) -> MemoryEntry:
        return cls(
            number=int(raw["number"]),
            content=str(raw.get("content") or ""),
            topic=raw.get("topic"),
            occurred_at=str(raw.get("occurred_at") or ""),
            source=str(raw.get("source") or ""),
            origin=raw.get("origin"),
            status=str(raw.get("status") or "active"),
            superseded_by=raw.get("superseded_by"),
            retired_at=raw.get("retired_at"),
            created_at=raw.get("created_at"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "content": self.content,
            "topic": self.topic,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "origin": self.origin,
            "status": self.status,
        }


class MemoryClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._transport = transport

    # ── 底层

    @staticmethod
    def _headers(as_user: UUID | str) -> dict[str, str]:
        uid = str(as_user)
        headers = {"X-Uid": uid, "X-Auth-Uid": uid, "X-Caller-App": caller_app()}
        ident = current_identity()
        if ident is not None and ident.tenant_id:
            headers["X-Tenant-Id"] = ident.tenant_id
        return headers

    async def _call(
        self,
        method: str,
        path: str,
        *,
        as_user: UUID | str,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=self._transport
            ) as client:
                r = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=body,
                    params=params,
                    headers=self._headers(as_user),
                )
        except httpx.HTTPError as exc:
            raise MemoryUnavailable(f"记忆服务不可达：{exc}") from exc
        if r.status_code == 204:
            return None
        if r.status_code >= 500:
            raise MemoryUnavailable(f"记忆服务返回 {r.status_code}：{r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise MemoryRejected(
                r.status_code,
                str(data.get("code") or ""),
                str(data.get("detail") or r.text[:200]),
                dict(data.get("context") or {}),
            )
        return data

    @staticmethod
    def _topic_path(scope: ScopeRef, code: str) -> str:
        return f"{scope.path}/topics/{quote(code, safe='')}"

    # ── 实例

    async def register(self, scope: ScopeRef, *, owner: UUID | str) -> dict[str, Any]:
        """登记实例归属（幂等）。记忆服务去用户中心写第一个 Owner（§3.7）。"""
        return await self._call(  # type: ignore[no-any-return]
            "POST", f"{scope.path}/register", as_user=owner, body={"owner_uid": str(owner)}
        )

    async def purge_instance(self, scope: ScopeRef, *, as_user: UUID | str) -> None:
        await self._call("DELETE", scope.path, as_user=as_user)

    # ── 主题

    async def catalog(self, scope: ScopeRef, *, as_user: UUID | str) -> list[MemoryTopic]:
        data = await self._call("GET", f"{scope.path}/topics", as_user=as_user)
        return [MemoryTopic.of(t) for t in data.get("topics") or []]

    async def load(
        self, scope: ScopeRef, code: str, *, as_user: UUID | str
    ) -> tuple[MemoryTopic, list[MemoryEntry]]:
        data = await self._call("GET", self._topic_path(scope, code), as_user=as_user)
        return MemoryTopic.of(data["topic"]), [MemoryEntry.of(e) for e in data.get("entries") or []]

    async def create_topic(
        self,
        scope: ScopeRef,
        code: str,
        description: str,
        *,
        attrs: dict[str, Any] | None = None,
        as_user: UUID | str,
    ) -> MemoryTopic:
        data = await self._call(
            "POST",
            f"{scope.path}/topics",
            as_user=as_user,
            body={"code": code, "description": description, "attrs": attrs or {}},
        )
        return MemoryTopic.of(data)

    async def update_topic(
        self,
        scope: ScopeRef,
        code: str,
        *,
        description: str | None = None,
        attrs: dict[str, Any] | None = None,
        as_user: UUID | str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if attrs is not None:
            payload["attrs"] = attrs
        return await self._call(  # type: ignore[no-any-return]
            "PATCH", self._topic_path(scope, code), as_user=as_user, body=payload
        )

    async def delete_topic(self, scope: ScopeRef, code: str, *, as_user: UUID | str) -> None:
        await self._call("DELETE", self._topic_path(scope, code), as_user=as_user)

    async def split(
        self,
        scope: ScopeRef,
        code: str,
        *,
        expected_version: int,
        groups: list[dict[str, Any]],
        as_user: UUID | str,
    ) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "POST",
            f"{self._topic_path(scope, code)}/split",
            as_user=as_user,
            body={"expected_version": expected_version, "groups": groups},
        )

    # ── 写入

    async def write(
        self,
        scope: ScopeRef,
        *,
        topic: str | None,
        content: str | None,
        remove: list[int],
        occurred_at: datetime,
        source: str,
        origin: dict[str, Any] | None,
        idempotency_key: str | None,
        as_user: UUID | str,
    ) -> dict[str, Any]:
        """一次写入：删除若干 + 至多新增一条（§3.5）。返回记忆服务的结果原样。"""
        return await self._call(  # type: ignore[no-any-return]
            "POST",
            f"{scope.path}/writes",
            as_user=as_user,
            body={
                "topic": topic,
                "content": content,
                "remove": remove,
                "occurred_at": occurred_at.isoformat(),
                "source": source,
                "origin": origin,
                "idempotency_key": idempotency_key,
            },
        )

    async def move(
        self, scope: ScopeRef, entries: list[int], to_topic: str, *, as_user: UUID | str
    ) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "POST",
            f"{scope.path}/moves",
            as_user=as_user,
            body={"entries": entries, "to_topic": to_topic},
        )

    # ── 条目与修订

    async def entries(
        self,
        scope: ScopeRef,
        *,
        as_user: UUID | str,
        status: str = "active",
        topic: str | None = None,
        source: str | None = None,
        origin: dict[str, Any] | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[MemoryEntry], int]:
        params: dict[str, Any] = {"status": status, "limit": limit, "offset": offset}
        if topic:
            params["topic"] = topic
        if source:
            params["source"] = source
        if origin:
            params["origin"] = json.dumps(origin, ensure_ascii=False)
        if q:
            params["q"] = q
        data = await self._call("GET", f"{scope.path}/entries", as_user=as_user, params=params)
        return [MemoryEntry.of(e) for e in data.get("data") or []], int(data.get("total") or 0)

    async def entry(self, scope: ScopeRef, number: int, *, as_user: UUID | str) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "GET", f"{scope.path}/entries/{number}", as_user=as_user
        )

    async def restore(
        self,
        scope: ScopeRef,
        number: int,
        *,
        topic: str | None,
        source: str,
        origin: dict[str, Any] | None,
        as_user: UUID | str,
    ) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "POST",
            f"{scope.path}/entries/{number}/restore",
            as_user=as_user,
            body={"topic": topic, "source": source, "origin": origin},
        )

    async def purge_entry(
        self, scope: ScopeRef, number: int, *, as_user: UUID | str
    ) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "DELETE", f"{scope.path}/entries/{number}", as_user=as_user
        )

    async def revisions(
        self,
        scope: ScopeRef,
        *,
        as_user: UUID | str,
        source: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source:
            params["source"] = source
        data = await self._call("GET", f"{scope.path}/revisions", as_user=as_user, params=params)
        return list(data.get("data") or [])

    async def undo(
        self,
        scope: ScopeRef,
        revision_id: str,
        *,
        source: str,
        origin: dict[str, Any] | None,
        as_user: UUID | str,
    ) -> dict[str, Any]:
        return await self._call(  # type: ignore[no-any-return]
            "POST",
            f"{scope.path}/revisions/{quote(revision_id, safe='')}/undo",
            as_user=as_user,
            body={"source": source, "origin": origin},
        )


def make_memory_client(settings: Any) -> MemoryClient | None:
    """没接入记忆服务就返回 None —— 调用方据此完全跳过记忆（不回落任何假实现）。"""
    if not settings.memory_base_url:
        return None
    return MemoryClient(settings.memory_base_url, timeout_s=settings.memory_timeout_s)
