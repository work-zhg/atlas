"""沙箱 · 通用供给（改造计划 B5，c4-model/agent/sandbox/code-sandbox-provisioning.html）。

对着真数据库、基座的 OpenSandboxRuntime 和假 OpenSandbox，业务类型用一个假的 SandboxKind：

    确保      先登记再建；已就绪的复用（每次向基座确认还能用）；坏了的退役后重建
    并发      同一个业务并发确保只建一个；同一个人并发新建不超过配额（验收）
    可重入    崩在「登记」和「建」之间，下一次用同一个 sandbox_uuid 接着建
    查询      current 只认 READY；confirm_gone 确实不在才去掉；describe / of_user / serving
    共享      共享键相同的业务共用一个沙箱，槽位各不相同；满了另建；最后一个释放才退役；
              current_of_user 退到这个人服务同类业务的沙箱
    回收      release 解绑后退役空出来的沙箱；reap_orphans 只释放不在活名单上的业务
    对账      重试卡在 RETIRING 的；基座里有、供给里没有登记的按 orphan 删掉
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import SandboxAccountRow, SandboxBindingRow, UserSandboxRow
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.repositories.sandbox_account import SandboxAccountRepository
from atlas_agent.sandbox.base.lifecycle.runtime import OpenSandboxRuntime
from atlas_agent.sandbox.base.lifecycle.spec import CreateSpec
from atlas_agent.sandbox.provisioning.model import (
    BizRef,
    BizType,
    QuotaExceeded,
    QuotaPolicy,
    SandboxRef,
    SandboxStatus,
    Sharing,
)
from atlas_agent.sandbox.provisioning.service import (
    SandboxProvisioning,
    SandboxUnavailableError,
)
from sqlalchemy import delete, select

from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import published_template, runtime_for, simple_spec


class FakeKind:
    """假的业务类型：记下被回调了什么；ready 可以设成失败。"""

    biz_type = BizType.NATIVE

    def __init__(self, template: str) -> None:
        self.template = template
        self.admitted: list[Any] = []
        self.attached: list[tuple[UUID, bool]] = []
        self.readied: list[UUID] = []
        self.fail_ready = False
        #: None = 独占（每个业务一个沙箱）
        self.sharing: Sharing | None = None

    async def admit(self, req: Any) -> None:
        if req == "denied":
            raise PermissionError("镜像不在白名单里")
        self.admitted.append(req)

    def build(self, req: Any, biz: BizRef) -> CreateSpec:
        return simple_spec(self.template, user=str(biz.user_uuid))

    def share(self, req: Any) -> Sharing | None:
        return self.sharing

    async def attach(self, ref: SandboxRef, req: Any, *, created: bool) -> None:
        self.attached.append((ref.sandbox_uuid, created))

    async def ready(self, ref: SandboxRef) -> None:
        if self.fail_ready:
            raise TimeoutError("没有就绪")
        self.readied.append(ref.sandbox_uuid)


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis() -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


@pytest.fixture
async def env(fake: FakeOpenSandbox, redis: Any) -> AsyncIterator[dict[str, Any]]:
    async with published_template() as template:
        runtime = runtime_for(fake, redis, template)
        kind = FakeKind(template.code)
        prov = SandboxProvisioning(runtime, get_sessionmaker(), quota=QuotaPolicy(2), cache_s=0.0)
        prov.register(kind)
        users: list[UUID] = []
        yield {"prov": prov, "kind": kind, "runtime": runtime, "users": users}
        async with get_sessionmaker()() as session:
            for user in users:
                ids = list(
                    await session.scalars(
                        select(UserSandboxRow.sandbox_uuid).where(UserSandboxRow.user_uuid == user)
                    )
                )
                await session.execute(
                    delete(SandboxBindingRow).where(SandboxBindingRow.sandbox_uuid.in_(ids))
                )
                await session.execute(
                    delete(UserSandboxRow).where(UserSandboxRow.user_uuid == user)
                )
                await session.execute(
                    delete(SandboxAccountRow).where(SandboxAccountRow.user_uuid == user)
                )
            await session.commit()


def _biz(env: dict[str, Any], user: UUID | None = None) -> BizRef:
    user = user or uuid7()
    if user not in env["users"]:
        env["users"].append(user)
    return BizRef(BizType.NATIVE, uuid7(), user)


async def test_ensure_registers_first_then_builds_and_reuses(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov, kind = env["prov"], env["kind"]
    biz = _biz(env)
    assert await prov.current(biz) is None
    ref = await prov.ensure(biz, "req")
    assert len(fake.created) == 1
    assert fake.created[0].metadata["atlas.sandbox"] == str(ref.sandbox_uuid)
    assert kind.attached == [(ref.sandbox_uuid, True)] and kind.readied == [ref.sandbox_uuid]
    assert await prov.current(biz) == ref
    described = await prov.describe(ref)
    assert described is not None and described.status is SandboxStatus.READY
    assert described.template_code == kind.template and described.serves(biz)

    assert await prov.ensure(biz, "req") == ref  # 复用：不再建，附加配置照样每次做
    assert len(fake.created) == 1
    assert kind.attached[-1] == (ref.sandbox_uuid, False)
    assert [r for _, r in await prov.serving(BizType.NATIVE) if r == ref] == [ref]


async def test_the_same_business_concurrently_gets_one_sandbox(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    fake.create_delay_s = 0.2
    biz = _biz(env)
    refs = await asyncio.gather(*(env["prov"].ensure(biz, "req") for _ in range(4)))
    assert len(set(refs)) == 1
    assert len(fake.created) == 1


async def test_one_person_never_exceeds_the_quota_even_concurrently(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    """验收（B5）：同一个人并发新建沙箱，不会超过配额（上限 2）。"""
    fake.create_delay_s = 0.1
    user = uuid7()
    bizs = [_biz(env, user) for _ in range(5)]
    results = await asyncio.gather(
        *(env["prov"].ensure(b, "req") for b in bizs), return_exceptions=True
    )
    made = [r for r in results if isinstance(r, SandboxRef)]
    refused = [r for r in results if isinstance(r, QuotaExceeded)]
    assert len(made) == 2 and len(refused) == 3, results
    assert len(fake.created) == 2
    assert len(await env["prov"].of_user(user)) == 2


async def test_admission_happens_before_anything_is_registered(env: dict[str, Any]) -> None:
    biz = _biz(env)
    with pytest.raises(PermissionError):
        await env["prov"].ensure(biz, "denied")
    assert await env["prov"].of_user(biz.user_uuid) == []


async def test_a_crash_between_registering_and_building_resumes_with_the_same_uuid(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov: SandboxProvisioning = env["prov"]
    biz = _biz(env)
    original = prov._bring_up

    async def crash(*_: Any) -> Any:
        raise RuntimeError("进程在交基座建之前崩了")

    prov._bring_up = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await prov.ensure(biz, "req")
    [registered] = await prov.of_user(biz.user_uuid)
    assert registered.status is SandboxStatus.CREATING and fake.created == []

    prov._bring_up = original  # type: ignore[method-assign]
    ref = await prov.ensure(biz, "req")
    assert ref.sandbox_uuid == registered.sandbox_uuid  # 接着建，不再登记一个新的
    assert len(fake.created) == 1


async def test_a_sandbox_that_never_gets_ready_is_retired_and_frees_the_quota(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    env["kind"].fail_ready = True
    biz = _biz(env)
    with pytest.raises(TimeoutError):
        await env["prov"].ensure(biz, "req")
    assert await env["prov"].of_user(biz.user_uuid) == []
    assert fake.sandboxes == {}  # 基座那边也删了


async def test_a_broken_ready_sandbox_is_replaced_on_the_next_ensure(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    biz = _biz(env)
    first = await env["prov"].ensure(biz, "req")
    fake.created[0].state = "Failed"  # bridge 崩了：Pod 进终态
    second = await env["prov"].ensure(biz, "req")
    assert second != first and len(fake.created) == 2
    assert [s.sandbox_uuid for s in await env["prov"].of_user(biz.user_uuid)] == [
        second.sandbox_uuid
    ]


async def test_confirm_gone_only_forgets_sandboxes_that_really_are_gone(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov = env["prov"]
    biz = _biz(env)
    ref = await prov.ensure(biz, "req")
    assert await prov.confirm_gone(biz, ref) is False  # 还在
    fake.vanish(fake.created[0].id)
    assert await prov.confirm_gone(biz, ref) is True
    assert await prov.current(biz) is None


async def test_release_unbinds_and_retires_the_vacant_sandbox(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov, runtime = env["prov"], env["runtime"]
    biz = _biz(env)
    ref = await prov.ensure(biz, "req")
    await prov.release(biz, reason="idle")
    assert await prov.current(biz) is None and await prov.describe(ref) is None
    assert fake.deleted == [fake.created[0].id]
    history = await runtime.history(ref.sandbox_uuid)
    assert history[-1].event_type == "destroyed" and history[-1].reason == "idle"


async def test_reap_orphans_releases_only_businesses_that_are_gone(env: dict[str, Any]) -> None:
    prov = env["prov"]
    alive, dead = _biz(env), _biz(env)
    await prov.ensure(alive, "req")
    await prov.ensure(dead, "req")
    others = [biz.biz_id for biz, _ in await prov.serving(BizType.NATIVE)]
    live = [i for i in others if i != dead.biz_id]
    assert await prov.reap_orphans(BizType.NATIVE, live) == 1
    assert await prov.current(alive) is not None and await prov.current(dead) is None


async def test_reconcile_retries_retiring_and_removes_unregistered_instances(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov: SandboxProvisioning = env["prov"]
    runtime: OpenSandboxRuntime = env["runtime"]
    biz = _biz(env)
    stuck = await prov.ensure(biz, "req")
    # 退役到一半崩了：标了 RETIRING，基座那边没删
    async with get_sessionmaker()() as session:
        repo = SandboxAccountRepository(session, cache_s=0)
        account = await repo.load(biz.user_uuid)
        account.retire(stuck.sandbox_uuid)
        await repo.save(account)
        await session.commit()
    # 基座里有、供给里没有登记的
    stray = uuid7()
    await runtime.launch(stray, simple_spec(env["kind"].template))

    await prov.reconcile()
    assert await prov.describe(stuck) is None
    assert await runtime.get(stuck.sandbox_uuid) is None
    assert await runtime.get(stray) is None
    assert fake.sandboxes == {}


async def test_a_launch_failure_surfaces_as_sandbox_unavailable(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    fake.create_state = "Failed"
    biz = _biz(env)
    with pytest.raises(SandboxUnavailableError, match="没有起来"):
        await env["prov"].ensure(biz, "req")
    assert await env["prov"].of_user(biz.user_uuid) == []


# ──────────────────────────────────────────────── 共享


async def test_one_person_shares_one_sandbox_with_distinct_slots(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    """共享键相同的业务共用一个沙箱：只建一个、只占一个配额，槽位各不相同。"""
    prov, kind = env["prov"], env["kind"]
    kind.sharing = Sharing()
    user = uuid7()
    a, b, c = (_biz(env, user) for _ in range(3))
    refs = [await prov.ensure(x, "req") for x in (a, b, c)]
    assert len(set(refs)) == 1 and len(fake.created) == 1
    assert [await prov.slot(x) for x in (a, b, c)] == [0, 1, 2]
    [sandbox] = await prov.of_user(user)
    assert sandbox.share_key is not None and len(sandbox.bindings) == 3

    # 中间的释放了，槽位空出来给下一个
    await prov.release(b, reason="release")
    d = _biz(env, user)
    assert await prov.ensure(d, "req") == refs[0]
    assert await prov.slot(d) == 1
    # 别人不共用
    other = _biz(env)
    assert await prov.ensure(other, "req") != refs[0]


async def test_concurrent_businesses_of_one_person_end_up_in_one_sandbox(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    """同一个人同时开几个会话：账户版本号 CAS 让它们挑同一个（在建的也挑），只建一个。"""
    fake.create_delay_s = 0.2
    env["kind"].sharing = Sharing()
    user = uuid7()
    bizs = [_biz(env, user) for _ in range(4)]
    refs = await asyncio.gather(*(env["prov"].ensure(b, "req") for b in bizs))
    assert len(set(refs)) == 1 and len(fake.created) == 1
    assert sorted([await env["prov"].slot(b) for b in bizs]) == [0, 1, 2, 3]


async def test_a_full_sandbox_makes_room_in_a_new_one(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    env["kind"].sharing = Sharing(capacity=2)
    user = uuid7()
    refs = [await env["prov"].ensure(_biz(env, user), "req") for _ in range(3)]
    assert refs[0] == refs[1] != refs[2]
    assert len(fake.created) == 2


async def test_different_share_keys_do_not_share(env: dict[str, Any]) -> None:
    """凭据集合这类业务给的键不同：各建各的。"""
    prov, kind = env["prov"], env["kind"]
    user = uuid7()
    kind.sharing = Sharing("creds:a")
    first = await prov.ensure(_biz(env, user), "req")
    kind.sharing = Sharing("creds:b")
    assert await prov.ensure(_biz(env, user), "req") != first


async def test_a_shared_sandbox_retires_only_after_the_last_release(
    env: dict[str, Any], fake: FakeOpenSandbox
) -> None:
    prov = env["prov"]
    env["kind"].sharing = Sharing()
    user = uuid7()
    a, b = _biz(env, user), _biz(env, user)
    ref = await prov.ensure(a, "req")
    await prov.ensure(b, "req")

    await prov.release(a, reason="idle")
    assert fake.deleted == [] and await prov.current(b) == ref
    await prov.release(b, reason="idle")
    assert len(fake.deleted) == 1 and await prov.of_user(user) == []


async def test_current_of_user_falls_back_to_the_persons_sandbox(env: dict[str, Any]) -> None:
    """文件读写的选路：业务自己没绑，就用这个人服务同类业务的 READY 沙箱（挂的是同一个前缀）。"""
    prov = env["prov"]
    env["kind"].sharing = Sharing()
    user = uuid7()
    bound, unbound = _biz(env, user), _biz(env, user)
    assert await prov.current_of_user(unbound) is None
    ref = await prov.ensure(bound, "req")
    assert await prov.current(unbound) is None
    assert await prov.current_of_user(unbound) == ref
    assert await prov.current_of_user(_biz(env)) is None  # 别人的不算
