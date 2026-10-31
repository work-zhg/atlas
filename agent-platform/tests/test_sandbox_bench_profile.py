"""评测档沙箱（profile=bench）与「在运行中的沙箱里直接执行」。

评测档是第三个 lifecycle server（opensandbox-bench：root、根可写）。沙箱仍是 sbx 这一种：
路由表、令牌、文件路由不分档；建按档位选 server，查 / 删 / 列两边都问 —— 于是复用、回收、
会话删除都不用知道档位。用两个假 lifecycle server（osb、osb-bench）对着验。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.domain.spec import (
    AgentSpec,
    CliSpec,
    ModelSpec,
    SandboxSpec,
    SubAgentSpec,
    spec_for_subagent,
)
from atlas_agent.providers.opensandbox import ImageDenied, SandboxProvisioner
from atlas_agent.providers.sandbox import (
    SandboxNotRunning,
    SandboxProfileDenied,
    exec_in_running,
)
from atlas_agent.redisx import make_redis
from atlas_engine.contracts import InvalidSpec
from pydantic import SecretStr

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings, wipe

USER = "u-1"
BENCH_IMAGE = "tb/regex-log:1"


@pytest.fixture
def strict(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path / "strict")
    yield f
    f.close()


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path / "bench", host="osb-bench")
    yield f
    f.close()


@pytest.fixture
def ws() -> str:
    return f"ws-{uuid4().hex[:10]}"


@pytest.fixture
async def redis(strict: FakeOpenSandbox, bench: FakeOpenSandbox, ws: str) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await wipe(client, ws, strict.prefix, bench.prefix)
    await client.aclose()


def _owner(strict: FakeOpenSandbox, bench: FakeOpenSandbox, request: httpx.Request) -> Any:
    host = request.url.host
    if host == bench.host or host.removesuffix(".execd") in bench.sandboxes:
        return bench
    return strict


def _transports(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox
) -> tuple[httpx.MockTransport, httpx.MockTransport]:
    """两个假 server 合成一组 transport：lifecycle 按主机名分，execd 按沙箱 id 分。"""

    async def handle_async(request: httpx.Request) -> httpx.Response:
        return await _owner(strict, bench, request)._handle_async(request)

    def handle(request: httpx.Request) -> httpx.Response:
        return _owner(strict, bench, request)._handle(request)

    return httpx.MockTransport(handle_async), httpx.MockTransport(handle)


def _settings(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "opensandbox_bench_url": "http://osb-bench",
        "opensandbox_bench_api_key": SecretStr("bench-key"),
        "sandbox_bench_image_allow_prefixes": ["tb/"],
    }
    return fake_settings(**(base | overrides))


def _p(strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, **kw: Any) -> Any:
    transport, _ = _transports(strict, bench)
    return SandboxProvisioner(_settings(**kw), redis, transport=transport)


# ──────────────────────────────────────────────── 建：按档位选 server


async def test_bench_profile_is_created_on_the_bench_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    ep = await _p(strict, bench, redis).ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    assert not strict.created
    [sb] = bench.created
    assert ep.sandbox_id == sb.id
    assert sb.metadata["atlas.profile"] == "bench"
    assert sb.spec["image"] == {"uri": BENCH_IMAGE}
    # ★ 评测档不覆盖 HOME：题目镜像可能在 root 的 HOME 里预置了东西
    assert "HOME" not in sb.spec["env"]


async def test_strict_profile_stays_on_the_strict_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    await _p(strict, bench, redis).ensure(ws, user=USER)
    assert not bench.created
    [sb] = strict.created
    assert sb.metadata["atlas.profile"] == "strict"
    assert sb.spec["env"]["HOME"] == "/home/agent"


async def test_only_bench_sandboxes_get_the_egress_proxy(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    proxy = "http://u:p@10.0.0.1:28118"
    p = _p(strict, bench, redis, sandbox_bench_proxy=SecretStr(proxy))
    await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.ensure(f"{ws}-s", user=USER)
    try:
        env = bench.created[0].spec["env"]
        assert env["https_proxy"] == env["HTTPS_PROXY"] == env["http_proxy"] == proxy
        assert env["no_proxy"] == env["NO_PROXY"] == "localhost,127.0.0.1,::1,.svc,.cluster.local"
        assert not {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"} & set(
            strict.created[0].spec["env"]
        )
    finally:
        await p.release(f"{ws}-s")


async def test_bench_profile_without_the_bench_server_is_denied(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(strict, bench, redis, opensandbox_bench_url=None)
    with pytest.raises(ImageDenied, match="没有评测档"):
        await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    assert not strict.created


async def test_bench_images_have_their_own_allowlist(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(strict, bench, redis, sandbox_image_allow_prefixes=["tb/"])
    with pytest.raises(ImageDenied, match="评测档"):
        await p.ensure(ws, user=USER, image="other/img:1", profile="bench")
    # 评测档的前缀不放行 strict 档
    p = _p(strict, bench, redis, sandbox_bench_image_allow_prefixes=["tb/"])
    with pytest.raises(ImageDenied):
        await p.ensure(ws, user=USER, image="tb/x:1", profile="strict")
    assert not strict.created and not bench.created


# ──────────────────────────────────────────────── 查 / 删 / 回收：两边都问


async def test_lost_route_finds_the_bench_sandbox(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(strict, bench, redis)
    ep = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.routes.delete(ws)
    again = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    assert again.sandbox_id == ep.sandbox_id
    assert len(bench.created) == 1


async def test_idle_reaping_and_release_reach_the_bench_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(strict, bench, redis, sandbox_idle_ttl_s=0.0)
    ep = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.reap_idle()
    assert bench.deleted == [ep.sandbox_id]
    assert await p.routes.get(ws, fresh=True) is None

    ep = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.release(ws)
    assert ep.sandbox_id in bench.deleted


async def test_orphans_on_the_bench_server_are_reaped(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(strict, bench, redis, sandbox_reap_min_age_s=0.0)
    ep = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.reap_orphans(live_threads=set())
    assert ep.sandbox_id in bench.deleted


async def test_unreachable_bench_server_does_not_block_strict_sandboxes(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """评测档是附加的：它连不上时，平台默认的沙箱照常建（配额、复用的列表都跳过它）。"""

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == bench.host:
            raise httpx.ConnectError("down", request=request)
        return await strict._handle_async(request)

    p = SandboxProvisioner(_settings(), redis, transport=httpx.MockTransport(handle))
    ep = await p.ensure(ws, user=USER)
    assert ep.sandbox_id == strict.created[0].id


async def test_shared_namespace_lists_each_sandbox_once(
    strict: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """部署形态：两个 server 在同一个命名空间建沙箱，彼此都列得到对方的。

    不按 id 去重的话：复用时「一个会话两个沙箱」会删掉正在用的那个；配额会数成两个。
    """

    async def handle(request: httpx.Request) -> httpx.Response:  # osb 与 osb-bench 是同一批 CR
        if request.url.host == "osb-bench":
            request = httpx.Request(
                request.method,
                request.url.copy_with(host=strict.host),
                headers=request.headers,
                content=request.content,
            )
        return await strict._handle_async(request)

    p = SandboxProvisioner(
        _settings(sandbox_quota_per_user=2), redis, transport=httpx.MockTransport(handle)
    )
    ep = await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    await p.routes.delete(ws)
    assert (await p.ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")) == ep
    assert not strict.deleted
    other = f"{ws}-2"
    try:
        await p.ensure(other, user=USER)  # 配额 2：已有 1 个，不能数成 2 个
    finally:
        await p.release(other)
    assert len(strict.created) == 2


# ──────────────────────────────────────────────── 直接执行


async def _exec(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str, **kw: Any
) -> Any:
    transport, sync = _transports(strict, bench)
    p = SandboxProvisioner(_settings(), redis, transport=transport)
    kw.setdefault("command", "echo hi")
    return await exec_in_running(
        p,
        ws,
        user_id=USER,
        profile="bench",
        poll_min_s=0.02,
        poll_max_s=0.05,
        transport=transport,
        files_transport=sync,
        **kw,
    )


async def test_exec_needs_a_running_sandbox_and_never_creates_one(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    with pytest.raises(SandboxNotRunning):
        await _exec(strict, bench, redis, ws)
    assert not strict.created and not bench.created


async def test_exec_refuses_strict_sandboxes(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    await _p(strict, bench, redis).ensure(ws, user=USER)
    with pytest.raises(SandboxProfileDenied):
        await _exec(strict, bench, redis, ws)


async def test_exec_writes_files_then_runs_in_the_existing_bench_sandbox(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    ep = await _p(strict, bench, redis).ensure(ws, user=USER, image=BENCH_IMAGE, profile="bench")
    res = await _exec(
        strict,
        bench,
        redis,
        ws,
        command="cat t/check.sh && exit 3",  # 假 execd 在宿主上跑 bash：cwd 换算成工作区目录
        files=[("/workspace/t/check.sh", b"echo checked\n")],
    )
    assert res.exit_code == 3
    assert "echo checked" in res.output
    assert [sb.id for sb in bench.created] == [ep.sandbox_id]


# ──────────────────────────────────────────────── 配置


def _model() -> ModelSpec:
    return ModelSpec(model="deepseek-v4-pro", provider="openai")


def test_native_subagent_inherits_the_sandbox_and_acp_one_does_not() -> None:
    parent = AgentSpec(
        slug="p",
        name="p",
        system_prompt="",
        model=_model(),
        tool_names=("bash",),
        subagents=(
            SubAgentSpec(name="n", description="", system_prompt="", model=_model()),
            SubAgentSpec(
                name="a",
                description="",
                system_prompt="",
                model=_model(),
                kind="acp",
                cli=CliSpec(cli_type="claude-code", image="img"),
            ),
        ),
        sandbox=SandboxSpec(image=BENCH_IMAGE, profile="bench"),
    )
    assert spec_for_subagent(parent, "n").sandbox == parent.sandbox
    assert spec_for_subagent(parent, "a").sandbox == SandboxSpec()


def test_acp_agent_rejects_a_sandbox_config() -> None:
    spec = AgentSpec(
        slug="a",
        name="a",
        system_prompt="",
        model=_model(),
        kind="acp",
        cli=CliSpec(cli_type="claude-code", image="img"),
        sandbox=SandboxSpec(image="x"),
    )
    with pytest.raises(InvalidSpec, match=r"cli\.image"):
        spec.validate()
