"""沙箱基座 · 沙箱内执行（改造计划 B3，c4-model/agent/sandbox/code-sandbox-base-execution.html）。

对着假 OpenSandbox 的 execd（真跑 bash）和只实现 access 的假生命周期：

    命令      后台提交 → init 拿到 id；游标读；状态；中断
    三种错误  SandboxGone（沙箱不在）、CommandGone（execd 的 404 和 400 两种回法合一）、
              SubmitUnknown（init 之前断开）
    缓存      地址和令牌在进程内缓存，连不上就作废，下一次重取
    活动      提交、结束、打开文件各记一次；账本里残留的命令逐个确认，划掉残留不算活动
    口径      usage 带统计口径
    文件      上传、下载、大小、递归列出、删除
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.config import get_settings
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.execution.execd import ExecdChannel
from atlas_agent.sandbox.base.execution.ledger import ActivityLedger
from atlas_agent.sandbox.base.execution.values import (
    CommandGone,
    CommandSpec,
    ExecdUnreachable,
    FileEntry,
    SandboxGone,
    SubmitUnknown,
)
from atlas_agent.sandbox.base.lifecycle.runtime import SandboxAccess
from pydantic import SecretStr

from tests.opensandbox_fake import FakeOpenSandbox, FakeSandbox


class FakeLifecycle:
    """只实现 access：sandbox_uuid → 假 OpenSandbox 里的一个沙箱。"""

    def __init__(self, fake: FakeOpenSandbox) -> None:
        self._fake = fake
        self.boxes: dict[UUID, str] = {}
        self.calls = 0

    def add(self) -> tuple[UUID, FakeSandbox]:
        sid = uuid7()
        sb = self._fake.add(
            {"metadata": {"atlas.thread": str(sid)}, "env": {"EXECD_ACCESS_TOKEN": f"tok-{sid}"}}
        )
        self.boxes[sid] = sb.id
        return sid, sb

    async def access(self, sandbox_uuid: UUID) -> SandboxAccess:
        self.calls += 1
        external = self.boxes.get(sandbox_uuid)
        if external is None or external not in self._fake.sandboxes:
            raise SandboxGone(str(sandbox_uuid))
        sb = self._fake.sandboxes[external]
        return SandboxAccess(f"http://{sb.id}.execd:44772", SecretStr(sb.token))


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis() -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    prefix = f"atlas:test-exec-{uuid7().hex[:8]}"
    client.test_prefix = prefix  # type: ignore[attr-defined]
    yield client
    async for key in client.scan_iter(match=f"{prefix}:*"):
        await client.delete(key)
    await client.aclose()


@pytest.fixture
def lifecycle(fake: FakeOpenSandbox) -> FakeLifecycle:
    return FakeLifecycle(fake)


@pytest.fixture
def ledger(redis: Any) -> ActivityLedger:
    return ActivityLedger(redis, prefix=redis.test_prefix)


@pytest.fixture
def channel(
    fake: FakeOpenSandbox, lifecycle: FakeLifecycle, ledger: ActivityLedger
) -> ExecdChannel:
    return ExecdChannel(
        lifecycle,
        ledger,
        usage_scope="node",
        transport=fake.async_transport,
        files_transport=fake.sync_transport,
    )


async def _until_done(channel: ExecdChannel, sid: UUID, cmd_id: str) -> tuple[bytes, int | None]:
    out, cursor = b"", 0
    for _ in range(200):
        chunk = await channel.read(sid, cmd_id, cursor)
        out, cursor = out + chunk.data, chunk.cursor
        status = await channel.status(sid, cmd_id)
        if not status.running:
            chunk = await channel.read(sid, cmd_id, cursor)
            return out + chunk.data, status.exit_code
        await asyncio.sleep(0.02)
    raise AssertionError("命令没有结束")


async def test_background_command_is_followed_by_cursor_until_exit(
    channel: ExecdChannel, lifecycle: FakeLifecycle, ledger: ActivityLedger
) -> None:
    sid, _ = lifecycle.add()
    assert await channel.ping(sid)
    cmd_id = await channel.start(sid, CommandSpec("echo hi; echo there; exit 3", cwd="/workspace"))
    assert await ledger.running(sid) == {cmd_id}
    out, code = await _until_done(channel, sid, cmd_id)
    assert out == b"hi\nthere\n" and code == 3
    assert await ledger.running(sid) == set()  # 结束：划掉


async def test_unknown_commands_are_command_gone_for_both_status_and_logs(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, sb = lifecycle.add()
    cmd_id = await channel.start(sid, CommandSpec("sleep 30", cwd="/workspace"))
    fake.restart_execd(sb.id)  # 容器重启：execd 的命令记录全丢
    with pytest.raises(CommandGone):
        await channel.status(sid, cmd_id)  # execd 回 404
    with pytest.raises(CommandGone):
        await channel.read(sid, cmd_id, 0)  # execd 回 400


async def test_a_vanished_sandbox_is_sandbox_gone(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, sb = lifecycle.add()
    assert await channel.ping(sid)
    fake.vanish(sb.id)
    with pytest.raises(ExecdUnreachable):
        await channel.status(sid, "cmd-1")  # 缓存的地址连不上：作废
    with pytest.raises(SandboxGone):
        await channel.status(sid, "cmd-1")  # 重取时生命周期说它不在了


async def test_the_address_cache_is_dropped_on_connection_errors(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, _ = lifecycle.add()
    cmd_id = await channel.start(sid, CommandSpec("echo ok", cwd="/workspace"))
    assert lifecycle.calls == 1
    await channel.status(sid, cmd_id)
    assert lifecycle.calls == 1  # 命中缓存
    fake.break_execd(1)
    with pytest.raises(ExecdUnreachable):
        await channel.status(sid, cmd_id)
    out, code = await _until_done(channel, sid, cmd_id)  # 同一个 id、同一个游标接着读
    assert (out, code) == (b"ok\n", 0)
    assert lifecycle.calls == 2


async def test_a_disconnect_before_init_is_submit_unknown(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, sb = lifecycle.add()
    fake.drop_init = True
    with pytest.raises(SubmitUnknown):
        await channel.start(sid, CommandSpec("echo maybe", cwd="/workspace"))
    assert len(sb.commands) == 1  # ★ 命令其实已经开始了：不能重试


async def test_usage_carries_its_scope(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, _ = lifecycle.add()
    fake.cpu_pct = 12.5
    usage = await channel.usage(sid)
    assert usage is not None and usage.cpu_pct == 12.5 and not usage.per_sandbox()
    fake.cpu_pct = None
    assert await channel.usage(sid) is None


async def test_activity_confirms_running_commands_and_drops_leftovers_quietly(
    channel: ExecdChannel, lifecycle: FakeLifecycle, ledger: ActivityLedger
) -> None:
    sid, _ = lifecycle.add()
    cmd_id = await channel.start(sid, CommandSpec("sleep 30", cwd="/workspace"))
    act = await channel.activity(sid)
    assert act.busy() and act.running == {cmd_id}

    await channel.interrupt(sid, cmd_id)
    await ledger.started(sid, "cmd-left-by-a-crashed-server")  # 进程崩在命令中途留下的
    before = await ledger.last_active(sid)
    await asyncio.sleep(0.05)
    for _ in range(50):
        act = await channel.activity(sid)
        if not act.busy():
            break
        await asyncio.sleep(0.05)
    assert not act.busy()
    assert await ledger.running(sid) == set()
    assert act.last_active_at == before  # ★ 划掉残留不算活动


async def test_activity_counts_unanswered_commands_as_running(
    channel: ExecdChannel, lifecycle: FakeLifecycle, fake: FakeOpenSandbox
) -> None:
    sid, _ = lifecycle.add()
    cmd_id = await channel.start(sid, CommandSpec("sleep 30", cwd="/workspace"))
    fake.break_execd(1)
    act = await channel.activity(sid)  # 问不到：当它还在跑，宁可晚回收
    assert act.running == {cmd_id}


async def test_files_round_trip_and_opening_counts_as_activity(
    channel: ExecdChannel, lifecycle: FakeLifecycle, ledger: ActivityLedger
) -> None:
    sid, _ = lifecycle.add()
    before = await ledger.last_active(sid)
    await asyncio.sleep(0.05)
    files = await channel.open_files(sid)
    try:
        assert files.size("/workspace/a/b.txt") is None
        assert files.download("/workspace/a/b.txt") is None
        files.upload("/workspace/a/b.txt", b"hello")
        files.upload("/workspace/c.txt", b"!")
        assert files.size("/workspace/a/b.txt") == 5
        assert files.download("/workspace/a/b.txt") == b"hello"
        assert files.search("/workspace") == [
            FileEntry("/workspace/a/b.txt", 5),
            FileEntry("/workspace/c.txt", 1),
        ]
        files.remove(["/workspace/c.txt"])
        assert [f.path for f in files.search("/workspace")] == ["/workspace/a/b.txt"]
        assert files.search("/workspace/missing") == []
    finally:
        files.close()
    assert await ledger.last_active(sid) > before
