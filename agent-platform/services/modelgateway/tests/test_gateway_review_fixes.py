"""针对架构与工程评审修复项的专项测试：
1. SSRF DNS 重绑定 IP 绑定建连 + 出网代理静态校验；
2. 租户级配置版本隔离（不跨租户重拉/重解密）+ Key 停用/删除零窗口即时生效；
3. 无会话客户端调用加权随机分流 + 全冷却半开单飞保护（429 MODEL_BUSY + Retry-After）；
4. 二进制 PDF 文档块跨格式防静默丢弃（OpenAI 拒绝 / Google 放行）+ redacted_thinking 溯源剥离；
5. 本地校验不误扣 RPM + Redis 丢失后当月花费懒重建 + cache_write 独立单价 + RecordQueue 毒丸隔离；
6. 账号探测按已配线路类型（向量/重排）与无模型列表供应商防假阳性。
"""

from __future__ import annotations

import json
import socket
from decimal import Decimal
from typing import Any

import httpcore
import httpx
import pytest
from atlas_modelgateway import notify
from atlas_modelgateway.accounts import AccountInput, AccountService
from atlas_modelgateway.crypto import SecretCipher
from atlas_modelgateway.db.models import GwApiKey, GwModelCall
from atlas_modelgateway.db.session import get_sessionmaker
from atlas_modelgateway.db.types import uuid7
from atlas_modelgateway.gateway.app import build_runtime, create_gateway_app
from atlas_modelgateway.gateway.config import ConfigSnapshot
from atlas_modelgateway.gateway.format.codecs import AnthropicCodec, OutContext
from atlas_modelgateway.gateway.metering import CallRecord, CostCalculator, RecordQueue
from atlas_modelgateway.provider import ProviderCatalog
from atlas_modelgateway.settings import get_settings
from atlas_modelgateway.shared.health import RouteHealth
from atlas_modelgateway.shared.kernel import (
    ParamPlan,
    Price,
    Usage,
    key_digest,
)
from atlas_modelgateway.shared.sync import VersionWatch
from atlas_modelgateway.shared.upstream import AddressGuard, UpstreamError, _GuardedBackend
from atlas_tenancy import TenantScope
from conftest import SECRET_KEY
from gw_testkit import (
    TENANT_A,
    TENANT_B,
    FakeUpstream,
    RouteSpec,
    anthropic_json,
    seed,
)
from sqlalchemy import select, update

# ── 1. SSRF DNS 重绑定 IP 绑定与代理模式检查 ─────────────────────────────


class _DummyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected_hosts: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self.connected_hosts.append((host, port))
        return "fake-stream"

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        raise AssertionError("unexpected unix socket")

    async def sleep(self, seconds: float) -> None:
        return None


async def test_guarded_backend_pins_validated_ip_against_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_getaddrinfo(host: str, port: int, **_: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        # 若发生二次解析，第二次故意返回 127.0.0.1 模拟 DNS 重绑定
        ip = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    inner = _DummyBackend()
    backend = _GuardedBackend(inner, AddressGuard())
    stream = await backend.connect_tcp("rebind.example.com", 443)
    assert stream == "fake-stream"
    assert calls == 1
    # 直接连向已校验的公网 IP 字面量，不给底层第二次解析的机会
    assert inner.connected_hosts == [("93.184.216.34", 443)]


async def test_guarded_backend_blocks_mixed_public_and_private_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def fake_getaddrinfo(host: str, port: int, **_: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    backend = _GuardedBackend(_DummyBackend(), AddressGuard())
    with pytest.raises(UpstreamError, match="内部网络"):
        await backend.connect_tcp("dual.example.com", 443)


def test_proxy_check_allows_overseas_domain_without_dns_and_blocks_cluster_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = AddressGuard()
    # 海外域名无需本机 DNS 可解即可通过代理预检
    guard.check_proxy("https://api.anthropic.com/v1/messages")
    guard.check_proxy("https://generativelanguage.googleapis.com/v1beta")
    # 集群内网域名、短主机名和内网 IP 一律拦截
    for bad in (
        "http://localhost:8030",
        "http://atlas-usercenter:8030",
        "http://atlas-usercenter.atlas-system.svc.cluster.local:8030",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1/internal",
    ):
        with pytest.raises(UpstreamError, match="内部网络"):
            guard.check_proxy(bad)


# ── 2. 租户级配置版本隔离与 Key 即时吊销 ──────────────────────────────────


class _FakeRedisDict:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.kv.get(k) for k in keys]

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        return True

    async def incr(self, key: str) -> int:
        val = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(val)
        return val

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def incrbyfloat(self, key: str, amount: float) -> float:
        val = float(self.kv.get(key, "0")) + amount
        self.kv[key] = str(val)
        return val

    async def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


async def test_tenant_isolated_config_versioning_and_zero_window_key_revocation() -> None:
    redis = _FakeRedisDict()
    notify.use_redis(redis)
    try:
        sa = await seed(TENANT_A, routes=[RouteSpec("a.test")])
        await seed(TENANT_B, routes=[RouteSpec("b.test")])

        watch = VersionWatch(redis, check_s=0.0)
        snap = ConfigSnapshot(get_sessionmaker(), SecretCipher([SECRET_KEY]), watch)

        cfg_a1 = await snap.tenant(TENANT_A)
        cfg_b1 = await snap.tenant(TENANT_B)
        key_a1 = await snap.key_by_digest(key_digest(sa.secret))
        assert key_a1 is not None and key_a1.status == "active"

        # 仅修改租户 A 的配置：租户 B 的内存快照引用保持不变（零重拉、零重解密）
        async with get_sessionmaker()() as session:
            notify.mark(session, tenant=TENANT_A, tenant_changed=True)
            await session.commit()
            await notify.flush(session)

        import asyncio

        _ = await snap.tenant(TENANT_A)  # 触发租户 A 后台热刷新
        await asyncio.sleep(0.05)
        cfg_a2 = await snap.tenant(TENANT_A)
        cfg_b2 = await snap.tenant(TENANT_B)
        assert cfg_a2.version != cfg_a1.version
        assert cfg_b2 is cfg_b1

        # 停用租户 A 的 Key：下一次认证零窗口立刻拿到 blocked（无需旧版循环重试）
        with TenantScope.enter(TENANT_A):
            async with get_sessionmaker()() as session:
                await session.execute(update(GwApiKey).values(status="blocked"))
                notify.mark(
                    session,
                    tenant=TENANT_A,
                    tenant_changed=False,
                    keys_changed=True,
                    evict_digests=[key_digest(sa.secret)],
                )
                await session.commit()
                await notify.flush(session)

        key_a2 = await snap.key_by_digest(key_digest(sa.secret))
        assert key_a2 is not None and key_a2.status == "blocked"
    finally:
        notify.use_redis(None)


# ── 3. 无状态客户端加权负载均衡 + 全冷却半开单飞保护 ──────────────────────


async def test_stateless_client_key_distributes_by_weight_and_session_pins() -> None:
    upstream = FakeUpstream()
    upstream.on("a.test", anthropic_json("A"))
    upstream.on("b.test", anthropic_json("B"))
    s = await seed(
        TENANT_A,
        routes=[
            RouteSpec("a.test", priority=1, weight=3),
            RouteSpec("b.test", priority=1, weight=1),
        ],
    )
    settings = get_settings()
    rt = build_runtime(settings, transport=upstream.transport(), guard=AddressGuard(enabled=False))
    app = create_gateway_app(settings, runtime=rt)
    body = {"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        for _ in range(60):
            r = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
            assert r.status_code == 200
        a_hits = len(upstream.hits("a.test"))
        b_hits = len(upstream.hits("b.test"))
        assert a_hits > 0 and b_hits > 0
        assert a_hits > b_hits  # weight 3:1

        # 带了 X-Session-Id 时严格粘连在同一条线路
        upstream.seen.clear()
        for _ in range(10):
            r = await c.post(
                "/v1/messages",
                headers={"x-api-key": s.secret, "x-session-id": "sess-fixed-1"},
                json=body,
            )
            assert r.status_code == 200
        assert {x.host for x in upstream.seen} in ({"a.test"}, {"b.test"})
    await rt.upstream.aclose()


async def test_all_cooling_half_open_probe_blocks_concurrent_stampede() -> None:
    redis = _FakeRedisDict()
    upstream = FakeUpstream()
    upstream.on("a.test", anthropic_json("ok"))
    s = await seed(TENANT_A, routes=[RouteSpec("a.test")])
    settings = get_settings()
    rt = build_runtime(
        settings, redis=redis, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    app = create_gateway_app(settings, runtime=rt)
    body = {"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        # 先预热租户拿到线路 UUID 并置为冷却 60 秒
        tcfg = await rt.config.tenant(TENANT_A)
        route_uuid = tcfg.models["m1"].routes[0].uuid
        await rt.health.cool(route_uuid, 60.0)

        # 第 1 个请求抢到半开探测权，放行探测
        r1 = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
        assert r1.status_code == 200
        assert len(upstream.hits("a.test")) == 1

        # 探测窗口内后续并发请求不再击穿冷却打上游，直接快速返回 429 MODEL_BUSY + Retry-After
        r2 = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=body)
        assert r2.status_code == 429
        assert r2.json()["error"]["category"] == "model_busy"
        assert int(r2.headers["retry-after"]) >= 1
        assert len(upstream.hits("a.test")) == 1
    await rt.upstream.aclose()


# ── 4. PDF 文档块防静默丢弃与 redacted_thinking 跨上游剥离 ────────────────


async def test_pdf_document_rejected_on_openai_only_and_routed_to_gemini_when_available() -> None:
    upstream = FakeUpstream()
    pdf_msg = {
        "model": "m_openai",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "JVBERi0xLjQK",
                        },
                    },
                    {"type": "text", "text": "总结这份合同"},
                ],
            }
        ],
    }
    s_oa = await seed(
        TENANT_A,
        model="m_openai",
        routes=[
            RouteSpec("oa.test", provider="deepseek", fmt="openai", upstream_model="deepseek-v4")
        ],
        caps={"tools": True, "vision": True},
    )
    s_mix = await seed(
        TENANT_A,
        model="m_mix",
        routes=[
            RouteSpec(
                "oa2.test",
                provider="deepseek",
                fmt="openai",
                upstream_model="deepseek-v4",
                priority=1,
            ),
            RouteSpec(
                "gm.test",
                provider="gemini",
                fmt="google",
                upstream_model="gemini-2.5-pro",
                priority=2,
            ),
        ],
        caps={"tools": True, "vision": True},
    )
    upstream.on(
        "gm.test",
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "已读完 PDF"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 5},
            },
        ),
    )
    settings = get_settings()
    rt = build_runtime(settings, transport=upstream.transport(), guard=AddressGuard(enabled=False))
    app = create_gateway_app(settings, runtime=rt)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        # 只有 OpenAI 线路：明确回 400 beyond_capability，绝不静默丢掉 PDF
        r1 = await c.post(
            "/universal/v1/messages", headers={"x-api-key": s_oa.secret}, json=pdf_msg
        )
        assert r1.status_code == 400
        assert r1.json()["error"]["category"] == "beyond_capability"
        assert upstream.hits("oa.test") == []

        # 同时有 OpenAI（优先级 1）和 Gemini（优先级 2）：自动跳过 OpenAI 走能接 PDF 的 Gemini
        r2 = await c.post(
            "/universal/v1/messages",
            headers={"x-api-key": s_mix.secret},
            json={**pdf_msg, "model": "m_mix"},
        )
        assert r2.status_code == 200
        assert r2.json()["content"][0]["text"] == "已读完 PDF"
        assert upstream.hits("oa2.test") == []
        sent = upstream.hits("gm.test")[0].body
        assert sent["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf"
    await rt.upstream.aclose()


def test_redacted_thinking_provenance_preserved_on_same_route_and_stripped_on_switch() -> None:
    codec = AnthropicCodec()
    raw_resp = {
        "id": "msg_1",
        "model": "claude-opus-5",
        "content": [
            {"type": "redacted_thinking", "data": "ENC_BLOB"},
            {"type": "text", "text": "答案"},
        ],
    }
    stamped = codec.outgoing_body(raw_resp, OutContext(model="m1", stamp="routeA"))
    assert stamped["content"][0]["data"] == "atlas1:routeA:ENC_BLOB"

    turn2 = {
        "model": "m1",
        "max_tokens": 100,
        "messages": [
            {"role": "assistant", "content": stamped["content"]},
            {"role": "user", "content": "继续"},
        ],
    }
    # 同一上游模型：去掉 atlas1 前缀还原原始加密块
    same = codec.apply(turn2, ParamPlan(upstream_model="claude-opus-5", stamp="routeA"))
    assert same["messages"][0]["content"][0] == {"type": "redacted_thinking", "data": "ENC_BLOB"}

    # 切到了别的线路：剥离别家的加密思考块，防止上游校验失败报 400
    switched = codec.apply(turn2, ParamPlan(upstream_model="deepseek-v4", stamp="routeB"))
    assert [b["type"] for b in switched["messages"][0]["content"]] == ["text"]


# ── 5. 配额扣减顺序、当月花费懒重建、cache_write 单价与毒丸隔离 ──────────


async def test_local_rejections_and_count_tokens_do_not_consume_rpm() -> None:
    redis = _FakeRedisDict()
    upstream = FakeUpstream()
    upstream.on("a.test", anthropic_json("ok"))
    s = await seed(TENANT_A, routes=[RouteSpec("a.test", fmt="openai", provider="deepseek")], rpm=1)
    settings = get_settings()
    rt = build_runtime(
        settings, redis=redis, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    app = create_gateway_app(settings, runtime=rt)
    msg = {"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    img_msg = {
        "model": "m1",
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AA"},
                    }
                ],
            }
        ],
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        # 1) 不支持看图 -> 400，不扣 RPM
        assert (
            await c.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=img_msg)
        ).status_code == 400
        # 2) /v1/messages 不转换 OpenAI 线路 -> 400，不扣 RPM
        assert (
            await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=msg)
        ).status_code == 400
        # 3) count_tokens -> 200，不扣对话 RPM
        assert (
            await c.post(
                "/universal/v1/messages/count_tokens", headers={"x-api-key": s.secret}, json=msg
            )
        ).status_code == 200

        # 真正有效的第 1 次对话仍然能通过 rpm=1！
        upstream.on(
            "a.test",
            lambda _: httpx.Response(
                200,
                json={
                    "id": "c1",
                    "model": "deepseek",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "好"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                },
            ),
        )
        assert (
            await c.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=msg)
        ).status_code == 200
        # 第 2 次有效对话触发 rpm=1 限流
        assert (
            await c.post("/universal/v1/messages", headers={"x-api-key": s.secret}, json=msg)
        ).status_code == 429
    await rt.upstream.aclose()


async def test_monthly_spend_reconstructed_from_db_after_redis_loss() -> None:
    redis = _FakeRedisDict()
    upstream = FakeUpstream()
    upstream.on("a.test", anthropic_json("ok", usage_in=1, usage_out=0))
    s = await seed(
        TENANT_A,
        routes=[RouteSpec("a.test")],
        price={"input": "1000000"},
        budget=Decimal("10"),
    )
    settings = get_settings()
    rt = build_runtime(
        settings, redis=redis, transport=upstream.transport(), guard=AddressGuard(enabled=False)
    )
    app = create_gateway_app(settings, runtime=rt)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c,
    ):
        # 模拟本月已经在库里花了 9.5 元，而 Redis 刚刚重启是空的
        from datetime import UTC, datetime

        with TenantScope.enter(TENANT_A):
            async with get_sessionmaker()() as session:
                session.add(
                    GwModelCall(
                        started_at=datetime.now(UTC),
                        call_uuid=uuid7(),
                        key_uuid=s.key_uuid,
                        key_kind="client",
                        model="m1",
                        entry_format="anthropic",
                        status="ok",
                        duration_ms=10,
                        cost=Decimal("9.500000"),
                    )
                )
                await session.commit()

        msg = {"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        # 第 1 次：懒重建读出 9.5 < 10，放行；完成后累加 1.0 -> 10.5
        r1 = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=msg)
        assert r1.status_code == 200
        import asyncio

        await asyncio.sleep(0.05)
        # 第 2 次：10.5 >= 10，拦住！
        r2 = await c.post("/v1/messages", headers={"x-api-key": s.secret}, json=msg)
        assert r2.status_code == 429
        assert await rt.admission.quota.spent(s.key_uuid, tenant_uuid=TENANT_A) == Decimal("10.5")
    await rt.upstream.aclose()


async def test_cache_write_price_and_record_queue_poison_isolation() -> None:
    u = Usage(input=1_000_000, cache_write=1_000_000, cache_hit=1_000_000, output=1_000_000)
    # 默认不传 cache_write：按输入价 4 算 -> 4 + 4 + 0.4 + 16 = 24.4
    p_default = Price.from_json({"input": "4", "cache_hit": "0.4", "output": "16"})
    assert CostCalculator.cost(u, p_default) == Decimal("24.400000")
    # 传了 cache_write=5（1.25 倍）-> 4 + 5 + 0.4 + 16 = 25.4
    p_cw = Price.from_json({"input": "4", "cache_write": "5", "cache_hit": "0.4", "output": "16"})
    assert CostCalculator.cost(u, p_cw) == Decimal("25.400000")
    assert json.loads(json.dumps(p_cw.to_json()))["cache_write"] == "5"

    # RecordQueue 毒丸隔离：中间夹一条唯一键冲突或脏数据的记录，前后正常记录照常入库
    from datetime import UTC, datetime

    from atlas_modelgateway.shared.partitions import PartitionKeeper

    sm = get_sessionmaker()
    await PartitionKeeper(sm).ensure()
    queue = RecordQueue(sm, batch=10)
    dup_uuid = uuid7()
    ts = datetime.now(UTC)
    r_ok1 = CallRecord(
        call_uuid=dup_uuid,
        tenant_uuid=TENANT_A,
        started_at=ts,
        model="m1\x00clean",
        entry_format="universal",
        status="ok",
        duration_ms=5,
        notes={"msg": "has\x00nul"},
    )
    # 相同 (call_uuid, started_at) 触发唯一约束冲突，模拟单条毒丸记录
    r_poison = CallRecord(
        call_uuid=dup_uuid,
        tenant_uuid=TENANT_A,
        started_at=ts,
        model="m1",
        entry_format="universal",
        status="ok",
        duration_ms=5,
    )
    r_ok2 = CallRecord(
        call_uuid=uuid7(),
        tenant_uuid=TENANT_A,
        started_at=ts,
        model="m2",
        entry_format="universal",
        status="ok",
        duration_ms=8,
    )
    queue.put(r_ok1)
    queue.put(r_poison)
    queue.put(r_ok2)
    written = await queue.flush()
    assert written == 2
    assert queue.dropped == 1
    assert queue.pending == 0
    with TenantScope.enter(TENANT_A):
        async with sm() as session:
            rows = list(await session.scalars(select(GwModelCall)))
            assert {r.model for r in rows} == {"m1clean", "m2"}


# ── 6. 账号探测：无模型列表供应商防空跑 + 向量模型走 /embeddings ──────────


async def test_account_probe_handles_no_list_models_and_embedding_routes() -> None:
    from atlas_modelgateway.shared.upstream import UpstreamClient

    upstream = FakeUpstream()
    upstream.on(
        "emb.test",
        lambda _: httpx.Response(
            200,
            json={"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}]},
        ),
    )
    s = await seed(
        TENANT_A,
        model="bge-m3",
        kind="embedding",
        routes=[
            RouteSpec("emb.test", provider="custom_openai", fmt="openai", upstream_model="bge-m3")
        ],
    )
    client = UpstreamClient(guard=AddressGuard(enabled=False), transport=upstream.transport())
    try:
        with TenantScope.enter(TENANT_A):
            async with get_sessionmaker()() as session:
                svc = AccountService(
                    session,
                    cipher=SecretCipher([SECRET_KEY]),
                    catalog=ProviderCatalog(),
                    guard=AddressGuard(enabled=False),
                    health=RouteHealth(None),
                )
                # 1) MiniMax（无模型列表、无余额接口）且没配线路与模型：不能空跑返回 ok=True
                mm = await svc.create(
                    AccountInput(
                        name="mm",
                        provider="minimax",
                        endpoints={"anthropic": "https://mm.test/anthropic"},
                        secret="sk-mm",
                    ),
                    actor="u1",
                )
                res_mm = await svc.probe(svc.ref(mm), client)
                assert res_mm.ok is False

                # 2) 已配 embedding 线路的账号：自动取线路模型走 /embeddings 探测而非对话接口
                emb_acc = await svc.get(s.accounts["emb.test"])
                # custom_openai 默认会先 GET /models，给它配一个空列表让它用已配线路探测
                upstream.script["emb.test"].insert(
                    0, lambda _: httpx.Response(200, json={"data": []})
                )
                res_emb = await svc.probe(svc.ref(emb_acc), client)
                assert res_emb.ok is True
                paths = [h.path for h in upstream.hits("emb.test")]
                assert "/openai/embeddings" in paths
    finally:
        await client.aclose()
