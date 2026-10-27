"""原生沙箱的供给与回收：SandboxProvisioner 对着假 lifecycle。

doc/sandbox-opensandbox-design.html §13、§14。ensure 的几条规矩都是 demo 撞出来的：
查了再建必须在锁里（创建请求不能指定 id）；Pending 要等不能删；令牌只有 Atlas 存着，
丢了只能重建；一个会话出现多个沙箱时留上次用的那个。
回收：闲置（有命令在跑不回收）、孤儿（会话已删，刚建好的除外）、会话删除时级联。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from atlas_agent.config import get_settings
from atlas_agent.providers.opensandbox import (
    ImageDenied,
    LifecycleError,
    QuotaExceeded,
    SandboxEndpoint,
    SandboxProvisioner,
    sandbox_spec,
)
from atlas_agent.redisx import make_redis

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings, wipe

USER = "u-1"


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
def ws() -> str:
    return f"ws-{uuid4().hex[:10]}"


@pytest.fixture
async def redis(fake: FakeOpenSandbox, ws: str) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await wipe(client, ws, fake.prefix)
    await client.aclose()


def _p(fake: FakeOpenSandbox, redis: Any, **settings: Any) -> SandboxProvisioner:
    return SandboxProvisioner(fake_settings(**settings), redis, transport=fake.async_transport)


def _spec(ws: str, *, user: str = USER, token: str = "tok") -> dict[str, Any]:
    return sandbox_spec(fake_settings(), ws=ws, user=user, token=token, image="img")


async def _existing(fake: FakeOpenSandbox, redis: Any, ws: str, **kw: Any) -> str:
    """别的进程建好的沙箱：在 lifecycle 里，令牌也在 Redis 里。"""
    sb = fake.add(_spec(ws), **kw)
    await redis.set(f"atlas:sbx:token:{sb.id}", "tok")
    return sb.id


# ──────────────────────────────────────────────── ensure


async def test_ensure_writes_the_route_and_reuses_it(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(fake, redis)
    ep = await p.ensure(ws, user=USER)
    assert ep.url == f"http://{ep.sandbox_id}.execd:44772"
    assert await p.routes.get(ws) == ep  # ★ 有路由 = 文件工具改走沙箱
    assert await p.ensure(ws, user=USER) == ep
    assert len(fake.created) == 1


async def test_lost_route_reuses_the_running_sandbox(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """路由表没了（Redis 清过、另一个进程）：按 metadata 找回原沙箱，不新建。"""
    sandbox_id = await _existing(fake, redis, ws)
    ep = await _p(fake, redis).ensure(ws, user=USER)
    assert ep.sandbox_id == sandbox_id
    assert ep.token == "tok"
    assert fake.created == []


async def test_concurrent_ensure_creates_one_sandbox(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """创建请求不能指定 id：没有锁的话两个 ensure 都会「没查到 → 建一个」。"""
    fake.create_delay_s = 0.3
    eps = await asyncio.gather(*(_p(fake, redis).ensure(ws, user=USER) for _ in range(3)))
    assert len({ep.sandbox_id for ep in eps}) == 1
    assert len(fake.created) == 1


async def test_pending_sandbox_is_waited_for_not_deleted(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    fake.pending_gets = 2
    sandbox_id = await _existing(fake, redis, ws, state="Pending")
    ep = await _p(fake, redis).ensure(ws, user=USER)
    assert ep.sandbox_id == sandbox_id
    assert fake.deleted == [] and fake.created == []


async def test_allocated_counts_as_usable(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    """Allocated = 有 IP、还没报 ready：上游创建时就把它当作可以接请求，列表里却原样返回。"""
    sandbox_id = await _existing(fake, redis, ws, state="Allocated")
    p = _p(fake, redis)
    ep = await p.ensure(ws, user=USER)
    assert ep.sandbox_id == sandbox_id
    assert fake.deleted == [] and fake.created == []
    assert await p.is_gone(ep) is False


async def test_sandbox_without_token_is_replaced(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """令牌只在 Atlas 的 Redis 里：丢了就连不上 execd，只能删掉重建。"""
    orphan = fake.add(_spec(ws))
    ep = await _p(fake, redis).ensure(ws, user=USER)
    assert fake.deleted == [orphan.id]
    assert ep.sandbox_id == fake.created[0].id


async def test_failed_sandbox_is_replaced(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    broken = await _existing(fake, redis, ws, state="Failed")
    ep = await _p(fake, redis).ensure(ws, user=USER)
    assert fake.deleted == [broken]
    assert ep.sandbox_id != broken
    assert await redis.get(f"atlas:sbx:token:{broken}") is None


async def test_duplicates_keep_the_one_in_use(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    """锁失效时可能建出两个：留路由指向的那个（文件、进程都在它上面），其余当场删。"""
    older = await _existing(fake, redis, ws, age_s=60)
    newer = await _existing(fake, redis, ws)
    p = _p(fake, redis)
    await p.routes.set(ws, SandboxEndpoint(older, "http://stale", "tok"))
    ep = await p.ensure(ws, user=USER, fresh=True)  # 连不上它时的重新 ensure
    assert ep.sandbox_id == older
    assert fake.deleted == [newer]


async def test_duplicates_without_route_keep_the_newest(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    older = await _existing(fake, redis, ws, age_s=60)
    newer = await _existing(fake, redis, ws)
    ep = await _p(fake, redis).ensure(ws, user=USER)
    assert ep.sandbox_id == newer
    assert fake.deleted == [older]


async def test_create_that_does_not_reach_running_is_deleted(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    fake.create_state = "Failed"
    with pytest.raises(LifecycleError, match="没有起来"):
        await _p(fake, redis).ensure(ws, user=USER)
    assert fake.deleted == [fake.created[0].id]
    assert await _p(fake, redis).routes.get(ws) is None


async def test_quota_counts_all_pages(fake: FakeOpenSandbox, redis: Any, ws: str) -> None:
    fake.page_size = 1
    for i in range(3):
        fake.add(_spec(f"{ws}-other{i}"))
    with pytest.raises(QuotaExceeded, match="已有 3 个沙箱"):
        await _p(fake, redis, sandbox_quota_per_user=3).ensure(ws, user=USER)
    assert fake.created == []
    fake.sandboxes[next(iter(fake.sandboxes))].state = "Stopping"  # 正在删的不占配额
    ep = await _p(fake, redis, sandbox_quota_per_user=3).ensure(ws, user=USER)
    assert ep.sandbox_id == fake.created[0].id
    other_user = await _p(fake, redis, sandbox_quota_per_user=3).ensure(f"{ws}-u2", user="u-2")
    assert other_user.sandbox_id == fake.created[1].id  # 配额按用户算


async def test_image_outside_the_allowlist_is_denied(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(fake, redis, sandbox_image_allow_prefixes=["registry.local/sandbox/"])
    with pytest.raises(ImageDenied):
        await p.ensure(ws, user=USER, image="docker.io/evil:latest")
    ep = await p.ensure(ws, user=USER, image="registry.local/sandbox/py:3.12")
    assert fake.created[0].spec["image"] == {"uri": "registry.local/sandbox/py:3.12"}
    assert ep.sandbox_id == fake.created[0].id


# ──────────────────────────────────────────────── 存活判断


async def test_is_gone_and_forget_only_the_matching_route(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(fake, redis)
    ep = await p.ensure(ws, user=USER)
    assert await p.is_gone(ep) is False
    await p.forget(ws, SandboxEndpoint("someone-else", "", ""))
    assert await p.routes.get(ws) == ep  # 路由已经指向别的沙箱时不能误删
    fake.vanish(ep.sandbox_id)
    assert await p.is_gone(ep) is True
    await p.forget(ws, ep)
    assert await p.routes.get(ws) is None


# ──────────────────────────────────────────────── 回收


async def test_idle_sandbox_is_reaped_route_first(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(fake, redis, sandbox_idle_ttl_s=60.0)
    ep = await p.ensure(ws, user=USER)
    await p.reap_idle()
    assert ep.sandbox_id not in fake.deleted  # 刚用过

    await redis.set(f"atlas:sbx:active:{ws}", str(time.time() - 120))
    await p.reap_idle()
    assert ep.sandbox_id in fake.deleted
    assert await p.routes.get(ws) is None
    assert await redis.get(f"atlas:sbx:active:{ws}") is None


async def test_running_command_blocks_idle_reaping(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    """不设运行上限：命令跑多久都不回收。残留的「运行中」记录（server 崩在命令中途）要能识别。"""
    from atlas_agent.providers.opensandbox import ExecdClient

    p = _p(fake, redis, sandbox_idle_ttl_s=60.0)
    ep = await p.ensure(ws, user=USER)
    cmd_id = await ExecdClient(ep.url, ep.token, transport=fake.async_transport).start(
        "sleep 30", cwd="/workspace"
    )
    await p.routes.started(ws, ep.sandbox_id, cmd_id)
    await p.routes.started(ws, ep.sandbox_id, "cmd-left-behind")
    await redis.set(f"atlas:sbx:active:{ws}", str(time.time() - 120))

    await p.reap_idle()
    assert fake.deleted == []
    assert await p.routes.running(ws) == {f"{ep.sandbox_id}|{cmd_id}"}  # 残留的那条被清掉

    fake.restart_execd(ep.sandbox_id)  # 命令没了
    await redis.set(f"atlas:sbx:active:{ws}", str(time.time() - 120))
    await p.reap_idle()
    assert fake.deleted == [ep.sandbox_id]


async def test_orphans_are_reaped_except_young_ones(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    live = await _existing(fake, redis, f"{ws}-live", age_s=3600)
    dead = await _existing(fake, redis, f"{ws}-dead", age_s=3600)
    young = await _existing(fake, redis, f"{ws}-young", age_s=5)
    p = _p(fake, redis, sandbox_reap_min_age_s=600.0)
    await p.routes.set(f"{ws}-dead", SandboxEndpoint(dead, "http://x", "tok"))
    await p.reap_orphans({f"{ws}-live"})
    assert fake.deleted == [dead]
    assert {sb.id for sb in fake.running()} >= {live, young}
    assert await p.routes.get(f"{ws}-dead") is None
    assert await redis.get(f"atlas:sbx:token:{dead}") is None


async def test_release_deletes_everything_of_the_workspace(
    fake: FakeOpenSandbox, redis: Any, ws: str
) -> None:
    p = _p(fake, redis)
    ep = await p.ensure(ws, user=USER)
    extra = await _existing(fake, redis, ws)
    await p.release(ws)
    assert sorted(fake.deleted) == sorted([ep.sandbox_id, extra])
    assert await p.routes.get(ws) is None
    assert await redis.get(f"atlas:sbx:active:{ws}") is None


async def test_release_swallows_lifecycle_failures(redis: Any, ws: str) -> None:
    """会话删除不能因为 OpenSandbox 不可达而失败：孤儿回收兜底。"""
    import httpx

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    p = SandboxProvisioner(fake_settings(), redis, transport=httpx.MockTransport(down))
    await p.release(ws)
