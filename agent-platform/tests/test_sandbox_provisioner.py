"""原生沙箱领域（改造计划 B6，c4-model/agent/sandbox/code-sandbox-native.html）。

整套沙箱组件对着假 OpenSandbox：

    确保      按工作区要沙箱、原样复用；镜像按档位准入；配额按人算（原生沙箱与会话 Pod 合计）
    查询      current 只认 READY、不新建；confirm_gone 确实不在才去掉
    回收      闲置 = 没有命令在跑且超过时限（残留的运行记录逐个向 execd 确认）；会话删除、孤儿回收
              只释放自己的业务；释放失败不挡会话删除
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.domain.spec import SandboxSpec
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.execution.ledger import ActivityLedger
from atlas_agent.sandbox.base.execution.values import CommandSpec
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.native import ImageDenied
from atlas_agent.sandbox.provisioning.model import QuotaExceeded

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings
from tests.sandbox_env import component, new_workspace


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


async def _age(redis: Any, sandbox_uuid: Any, seconds: float) -> None:
    """把这个沙箱的最近活动拨回 seconds 秒前。"""
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).timestamp()
    await redis.set(f"atlas:sandbox:{sandbox_uuid}:active", stamp)


async def test_ensure_is_per_workspace_and_reuses(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis)
    ws, other = new_workspace(), new_workspace()
    first = await c.native.ensure(ws, SandboxSpec())
    assert await c.native.ensure(ws, SandboxSpec()) == first
    assert await c.native.current(ws) == first
    assert await c.native.current(other) is None  # 不会触发新建
    assert len(fake.created) == 1
    assert await c.native.profile_of(first) == "strict"
    # 改了镜像也不换：已有的原样复用，闲置回收之后才按新配置建
    assert await c.native.ensure(ws, SandboxSpec(image="atlas-sandbox:other")) == first


async def test_images_are_admitted_per_profile(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis, sandbox_image_allow_prefixes=["registry.local/sandbox/"])
    with pytest.raises(ImageDenied):
        await c.native.ensure(new_workspace(), SandboxSpec(image="docker.io/evil:latest"))
    with pytest.raises(ImageDenied, match="没有评测档"):
        await c.native.ensure(new_workspace(), SandboxSpec(profile="bench"))
    await c.native.ensure(new_workspace(), SandboxSpec(image="registry.local/sandbox/py:3.12"))
    assert fake.created[0].spec["image"] == {"uri": "registry.local/sandbox/py:3.12"}


async def test_quota_counts_per_person(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis, sandbox_quota_per_user=1)
    ws = new_workspace()
    await c.native.ensure(ws, SandboxSpec())
    same_person = type(ws)(new_workspace().root_thread_uuid, ws.user_uuid)
    with pytest.raises(QuotaExceeded):
        await c.native.ensure(same_person, SandboxSpec())
    await c.native.ensure(new_workspace(), SandboxSpec())  # 别人不受影响
    assert len(fake.created) == 2


async def test_confirm_gone_forgets_only_vanished_sandboxes(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    c = component(fake, redis)
    ws = new_workspace()
    ref = await c.native.ensure(ws, SandboxSpec())
    assert await c.native.confirm_gone(ws, ref) is False
    fake.vanish(fake.created[0].id)
    assert await c.native.confirm_gone(ws, ref) is True
    assert await c.native.current(ws) is None


async def test_idle_sandboxes_are_released(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis, sandbox_idle_ttl_s=60.0)
    ws = new_workspace()
    ref = await c.native.ensure(ws, SandboxSpec())
    assert await c.native.reap_idle() == 0  # 刚用过
    await _age(redis, ref.sandbox_uuid, 120)
    assert await c.native.reap_idle() == 1
    assert fake.deleted == [fake.created[0].id]
    assert await c.native.current(ws) is None
    history = await c.runtime.history(ref.sandbox_uuid)
    assert history[-1].reason == "idle"


async def test_a_running_command_blocks_idle_reaping(fake: FakeOpenSandbox, redis: Any) -> None:
    """不设运行上限：命令跑多久都不回收。残留的「运行中」记录（server 崩在命令中途）要能识别。"""
    c = component(fake, redis, sandbox_idle_ttl_s=60.0)
    ws = new_workspace()
    ref = await c.native.ensure(ws, SandboxSpec())
    cmd_id = await c.exec.start(ref.sandbox_uuid, CommandSpec("sleep 30", cwd="/workspace"))
    await ActivityLedger(redis).started(ref.sandbox_uuid, "cmd-left-behind")
    await _age(redis, ref.sandbox_uuid, 120)

    assert await c.native.reap_idle() == 0
    assert fake.deleted == []
    assert await ActivityLedger(redis).running(ref.sandbox_uuid) == {cmd_id}  # 残留的那条被划掉

    fake.restart_execd(fake.created[0].id)  # 命令没了
    await _age(redis, ref.sandbox_uuid, 120)
    assert await c.native.reap_idle() == 1
    assert fake.deleted == [fake.created[0].id]


async def test_orphan_workspaces_are_released(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis)
    live, dead = new_workspace(), new_workspace()
    await c.native.ensure(live, SandboxSpec())
    await c.native.ensure(dead, SandboxSpec())
    assert await c.native.reap_orphans([live.root_thread_uuid]) == 1
    assert await c.native.current(live) is not None
    assert await c.native.current(dead) is None
    assert len(fake.deleted) == 1


async def test_release_deletes_the_workspace_sandbox(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis)
    ws = new_workspace()
    ref = await c.native.ensure(ws, SandboxSpec())
    await c.native.release(ws)
    assert fake.deleted == [fake.created[0].id]
    assert await c.native.current(ws) is None
    assert (await c.runtime.history(ref.sandbox_uuid))[-1].reason == "release"


async def test_release_swallows_lifecycle_failures(fake: FakeOpenSandbox, redis: Any) -> None:
    """会话删除不能因为 OpenSandbox 不可达而失败：孤儿回收和对账兜底。"""
    c = component(fake, redis)
    ws = new_workspace()
    await c.native.ensure(ws, SandboxSpec())

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    broken = SandboxComponent.build(fake_settings(), redis, transport=httpx.MockTransport(down))
    await broken.native.release(ws)
