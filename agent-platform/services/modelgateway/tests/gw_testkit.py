"""模型网关测试的替身和造数：假供应商（httpx.MockTransport）、往测试库里登记账号、模型、线路、Key。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from atlas_modelgateway.crypto import SecretCipher
from atlas_modelgateway.db.models import GwAccount, GwApiKey, GwModel, GwRoute
from atlas_modelgateway.db.session import get_sessionmaker
from atlas_modelgateway.gateway.admission import key_digest
from atlas_tenancy import TenantScope
from conftest import SECRET_KEY

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

Handler = Callable[[httpx.Request], httpx.Response]


# ── 假供应商的回答 ──────────────────────────────────────────────────────


def sse(
    events: Sequence[tuple[str | None, Any]], *, delay_s: float = 0.0, status: int = 200
) -> Handler:
    """一串 SSE 事件。delay_s > 0 时每段之间停一下（测保活、超时）。"""

    def encode(name: str | None, data: Any) -> bytes:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return ((f"event: {name}\n" if name else "") + f"data: {text}\n\n").encode()

    def handler(_: httpx.Request) -> httpx.Response:
        if not delay_s:
            return httpx.Response(
                status,
                headers={"content-type": "text/event-stream"},
                content=b"".join(encode(n, d) for n, d in events),
            )

        async def body() -> AsyncIterator[bytes]:
            for n, d in events:
                await asyncio.sleep(delay_s)
                yield encode(n, d)

        return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body())

    return handler


def anthropic_events(
    text: str = "你好", *, usage_in: int = 100, usage_out: int = 20, model: str = "up"
) -> list[tuple[str, Any]]:
    return [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {
                        "input_tokens": usage_in,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 1,
                    },
                },
            },
        ),
        ("ping", {"type": "ping"}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": usage_out},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def anthropic_json(
    text: str = "你好", *, usage_in: int = 100, usage_out: int = 20, cache_hit: int = 0
) -> Handler:
    return lambda _: httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "up",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": usage_in,
                "cache_read_input_tokens": cache_hit,
                "output_tokens": usage_out,
            },
        },
    )


def error(status: int, body: Any, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx.Response(status, json=body, headers=headers or {})


@dataclass
class Seen:
    host: str
    path: str
    body: Any
    headers: httpx.Headers


class FakeUpstream:
    """按主机名分派：每个主机一串回答，按顺序用，最后一个一直用下去。"""

    def __init__(self) -> None:
        self.script: dict[str, list[Handler]] = {}
        self.seen: list[Seen] = []

    def on(self, host: str, *handlers: Handler) -> None:
        self.script.setdefault(host, []).extend(handlers)

    def hits(self, host: str) -> list[Seen]:
        return [s for s in self.seen if s.host == host]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append(Seen(request.url.host, request.url.path, body, request.headers))
        queue = self.script.get(request.url.host)
        if not queue:
            return httpx.Response(
                599, json={"error": {"message": f"没有给 {request.url.host} 准备回答"}}
            )
        handler = queue.pop(0) if len(queue) > 1 else queue[0]
        return handler(request)


# ── 造数 ────────────────────────────────────────────────────────────────


@dataclass
class RouteSpec:
    host: str
    provider: str = "anthropic"
    fmt: str = "anthropic"
    upstream_model: str = "claude-opus-5"
    priority: int = 1
    weight: int = 1
    account: str = ""
    first_content_timeout_s: int | None = None


@dataclass
class Seeded:
    secret: str
    key_uuid: UUID
    model_uuid: UUID
    accounts: dict[str, UUID] = field(default_factory=dict)


async def seed(
    tenant: UUID,
    *,
    model: str = "m1",
    routes: Sequence[RouteSpec] = (),
    caps: dict[str, Any] | None = None,
    price: dict[str, Any] | None = None,
    key_models: Sequence[str] | None = None,
    budget: Decimal | None = None,
    rpm: int | None = None,
    enabled: bool = True,
    kind: str = "chat",
) -> Seeded:
    cipher = SecretCipher([SECRET_KEY])
    secret = f"sk-{uuid.uuid4().hex}"
    accounts: dict[str, UUID] = {}
    with TenantScope.enter(tenant):
        async with get_sessionmaker()() as session:
            m = GwModel(
                name=model,
                display_name=model,
                kind=kind,
                enabled=enabled,
                visibility="public",
                caps=caps
                if caps is not None
                else {"kind": kind, "tools": True, "vision": False, "thinking": "optional"},
                price=price or {},
            )
            session.add(m)
            await session.flush()
            for spec in routes:
                name = spec.account or spec.host
                if name not in accounts:
                    account = GwAccount(
                        name=name,
                        provider=spec.provider,
                        endpoints={spec.fmt: f"https://{spec.host}/{spec.fmt}"},
                        secret_cipher=cipher.encrypt(f"upstream-secret-{name}"),
                        first_content_timeout_s=spec.first_content_timeout_s,
                    )
                    session.add(account)
                    await session.flush()
                    accounts[name] = account.uuid
                session.add(
                    GwRoute(
                        model_uuid=m.uuid,
                        account_uuid=accounts[name],
                        upstream_model=spec.upstream_model,
                        priority=spec.priority,
                        weight=spec.weight,
                    )
                )
            key = GwApiKey(
                key_type="client",
                client_type="Agent",
                client_code=uuid.uuid4().hex,
                name="测试智能体",
                models=list(key_models if key_models is not None else [model]),
                effective_models=list(key_models if key_models is not None else [model]),
                status="active",
                secret=secret,
                secret_digest=key_digest(secret),
                secret_tail=secret[-4:],
                monthly_budget=budget,
                rpm=rpm,
            )
            session.add(key)
            await session.commit()
            return Seeded(secret, key.uuid, m.uuid, accounts)
