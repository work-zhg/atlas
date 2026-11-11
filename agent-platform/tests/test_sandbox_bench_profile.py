"""评测档沙箱（profile=bench）与「在运行中的沙箱里直接执行」（改造计划 B6）。

档位就是模板：strict → sbx-strict（opensandbox-sbx），bench → bench（第三个 lifecycle server
opensandbox-bench：root、根可写）。基座按模板绑定的 server 建、查、删，通用供给和原生沙箱不认识
server；回收、会话删除都不用知道档位。用两个假 lifecycle server（osb、osb-bench）对着验。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.exec import (
    PollingPolicy,
    SandboxNotRunning,
    SandboxProfileDenied,
    exec_in_running,
)
from atlas_agent.sandbox.native import ImageDenied, WorkspaceRef
from atlas_engine.contracts import InvalidSpec
from atlas_engine.contracts.spec import (
    AgentSpec,
    CliSpec,
    ModelSpec,
    SandboxSpec,
    SubAgentSpec,
    spec_for_subagent,
)
from pydantic import SecretStr

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings
from tests.sandbox_env import new_workspace

BENCH_IMAGE = "tb/regex-log:1"
BENCH = SandboxSpec(image=BENCH_IMAGE, profile="bench")


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
def ws() -> WorkspaceRef:
    return new_workspace()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
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


def _c(
    strict: FakeOpenSandbox,
    bench: FakeOpenSandbox,
    redis: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    **kw: Any,
) -> SandboxComponent:
    async_t, sync_t = _transports(strict, bench)
    return SandboxComponent.build(
        _settings(**kw), redis, transport=transport or async_t, files_transport=sync_t
    )


# ──────────────────────────────────────────────── 建：档位选模板，模板绑 server


async def test_bench_profile_is_created_on_the_bench_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis)
    ref = await c.native.ensure(ws, BENCH)
    assert not strict.created
    [sb] = bench.created
    assert sb.metadata["atlas.sandbox"] == str(ref.sandbox_uuid)
    assert sb.spec["image"] == {"uri": BENCH_IMAGE}
    assert await c.native.profile_of(ref) == "bench"
    # ★ 评测档不覆盖 HOME：题目镜像可能在 root 的 HOME 里预置了东西
    assert "HOME" not in sb.spec["env"]


async def test_strict_profile_stays_on_the_strict_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis)
    ref = await c.native.ensure(ws, SandboxSpec())
    assert not bench.created
    [sb] = strict.created
    assert await c.native.profile_of(ref) == "strict"
    assert sb.spec["env"]["HOME"] == "/home/agent"


async def test_every_native_sandbox_carries_an_egress_policy(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any
) -> None:
    """★ 每个沙箱都带出站策略（默认放行）：有策略才会加 egress sidecar。strict 档的 sidecar 镜像
    用 deny.always 拒绝集群网段；评测档的 server 用不带底线的 egress（它的出网代理在集群内）。"""
    c = _c(strict, bench, redis)
    await c.native.ensure(new_workspace(), SandboxSpec())
    await c.native.ensure(new_workspace(), BENCH)
    assert strict.created[0].spec["networkPolicy"] == {"defaultAction": "allow"}
    assert bench.created[0].spec["networkPolicy"] == {"defaultAction": "allow"}


async def test_only_bench_sandboxes_get_the_egress_proxy(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any
) -> None:
    proxy = "http://u:p@10.0.0.1:28118"
    c = _c(strict, bench, redis, sandbox_bench_proxy=SecretStr(proxy))
    await c.native.ensure(new_workspace(), BENCH)
    await c.native.ensure(new_workspace(), SandboxSpec())
    env = bench.created[0].spec["env"]
    assert env["https_proxy"] == env["HTTPS_PROXY"] == env["http_proxy"] == proxy
    assert env["no_proxy"] == env["NO_PROXY"] == "localhost,127.0.0.1,::1,.svc,.cluster.local"
    assert not {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"} & set(
        strict.created[0].spec["env"]
    )


async def test_bench_profile_without_the_bench_server_is_denied(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis, opensandbox_bench_url=None)
    with pytest.raises(ImageDenied, match="没有评测档"):
        await c.native.ensure(ws, BENCH)
    assert not strict.created


async def test_bench_images_have_their_own_allowlist(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis, sandbox_image_allow_prefixes=["tb/"])
    with pytest.raises(ImageDenied, match="评测档"):
        await c.native.ensure(ws, SandboxSpec(image="other/img:1", profile="bench"))
    # 评测档的前缀不放行 strict 档
    c = _c(strict, bench, redis, sandbox_bench_image_allow_prefixes=["tb/"])
    with pytest.raises(ImageDenied):
        await c.native.ensure(ws, SandboxSpec(image="tb/x:1"))
    assert not strict.created and not bench.created


# ──────────────────────────────────────────────── 复用 / 回收：按实例的模板找 server


async def test_reuse_idle_reaping_and_release_reach_the_bench_server(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis, sandbox_idle_ttl_s=0.0)
    ref = await c.native.ensure(ws, BENCH)
    assert await c.native.ensure(ws, BENCH) == ref
    assert len(bench.created) == 1
    assert await c.native.reap_idle() == 1
    assert bench.deleted == [bench.created[0].id]
    assert await c.native.current(ws) is None

    await c.native.ensure(ws, BENCH)
    await c.native.release(ws)
    assert bench.created[1].id in bench.deleted


async def test_orphans_on_the_bench_server_are_released(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis)
    await c.native.ensure(ws, BENCH)
    assert await c.native.reap_orphans([]) == 1
    assert bench.deleted == [bench.created[0].id]


async def test_an_unreachable_bench_server_does_not_block_strict_sandboxes(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """评测档是附加的：它连不上时，平台默认的沙箱照常建，对账也照常做（跳过它，不误删它的登记）。"""
    c0 = _c(strict, bench, redis)
    bench_ws = new_workspace()
    await c0.native.ensure(bench_ws, BENCH)

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == bench.host:
            raise httpx.ConnectError("down", request=request)
        return await _owner(strict, bench, request)._handle_async(request)

    c = _c(strict, bench, redis, transport=httpx.MockTransport(handle))
    await c.native.ensure(ws, SandboxSpec())
    assert len(strict.created) == 1
    await c.runtime.reconcile()
    await c.provisioning.reconcile()
    assert await c.native.current(bench_ws) is not None  # 评测档的那个没被当成孤儿
    assert not bench.deleted and not strict.deleted


async def test_a_shared_namespace_is_reconciled_without_deleting_anything(
    strict: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    """部署形态：两个 server 在同一个命名空间建沙箱，彼此都列得到对方的。

    对账按 OpenSandbox 的 id 去重：不去重的话，从另一个 server 列到的会被当成没登记的删掉。
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

    c = SandboxComponent.build(
        _settings(), redis, transport=httpx.MockTransport(handle), files_transport=None
    )
    await c.native.ensure(ws, BENCH)
    await c.native.ensure(new_workspace(), SandboxSpec())
    await c.runtime.reconcile()
    await c.provisioning.reconcile()
    assert not strict.deleted and len(strict.created) == 2


# ──────────────────────────────────────────────── 直接执行


async def _exec(c: SandboxComponent, ws: WorkspaceRef, **kw: Any) -> Any:
    kw.setdefault("command", "echo hi")
    return await exec_in_running(
        c.native, c.exec, c.ledger, ws, profile="bench", polling=PollingPolicy(0.02, 0.05), **kw
    )


async def test_exec_needs_a_running_sandbox_and_never_creates_one(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    with pytest.raises(SandboxNotRunning):
        await _exec(_c(strict, bench, redis), ws)
    assert not strict.created and not bench.created


async def test_exec_refuses_strict_sandboxes(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis)
    await c.native.ensure(ws, SandboxSpec())
    with pytest.raises(SandboxProfileDenied):
        await _exec(c, ws)


async def test_exec_writes_files_then_runs_in_the_existing_bench_sandbox(
    strict: FakeOpenSandbox, bench: FakeOpenSandbox, redis: Any, ws: WorkspaceRef
) -> None:
    c = _c(strict, bench, redis)
    await c.native.ensure(ws, BENCH)
    res = await _exec(
        c,
        ws,
        command="cat t/check.sh && exit 3",  # 假 execd 在宿主上跑 bash：cwd 换算成工作区目录
        files=[(f"{ws.layout().workdir}/t/check.sh", b"echo checked\n")],
    )
    assert res.exit_code == 3
    assert "echo checked" in res.output
    assert len(bench.created) == 1


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
                kind="cli",
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
        kind="cli",
        cli=CliSpec(cli_type="claude-code", image="img"),
        sandbox=SandboxSpec(image="x"),
    )
    with pytest.raises(InvalidSpec, match=r"cli\.image"):
        spec.validate()
