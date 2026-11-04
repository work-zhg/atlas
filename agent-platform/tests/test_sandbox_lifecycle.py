"""沙箱基座 · 生命周期（改造计划 B2，c4-model/agent/sandbox/code-sandbox-base-lifecycle.html）。

值对象      CreateSpec / Labels 构造时校验、policy_json 总是写出 defaultAction、状态分组
模板        TemplatePolicy 的准入规则；SandboxTemplates 新建、修订、发布、查漂移
实例        OpenSandboxRuntime 对着假 OpenSandbox 和真数据库：先登记再建、同一 uuid 只建一次、
            崩在「建好」和「回填」之间按标签找回、建失败清理、刷新状态只在变化时记事件、
            destroy 必须带原因、对账删掉没有登记的实例（存量沙箱也在其中）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import SandboxEvent, SandboxInstance, SandboxTemplate
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.base.lifecycle.kube import KubeApi
from atlas_agent.sandbox.base.lifecycle.runtime import (
    OpenSandboxRuntime,
    SandboxGone,
    TemplateUnavailable,
)
from atlas_agent.sandbox.base.lifecycle.server import LifecycleError, LifecycleServer, request_body
from atlas_agent.sandbox.base.lifecycle.spec import CreateSpec, Labels, Resources, Volume
from atlas_agent.sandbox.base.lifecycle.state import SandboxState
from atlas_agent.sandbox.base.lifecycle.templates import (
    SHA_ANNOTATION,
    KubeTemplatePublisher,
    SandboxTemplates,
    TemplateConflict,
    TemplatePolicy,
    TemplateRejected,
)
from atlas_agent.sandbox.base.network.policy import NetworkPolicy, policy_json
from pydantic import SecretStr
from sqlalchemy import delete, select

from tests.opensandbox_fake import LIFECYCLE_URL, FakeOpenSandbox

ADMIN = UUID(int=7)
GOOD = """
spec:
  template:
    spec:
      securityContext:
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: sandbox
"""

# ──────────────────────────────────────────────── 值对象


def _spec(template: str = "t", **kw: Any) -> CreateSpec:
    base: dict[str, Any] = {
        "template": template,
        "image": "img",
        "entrypoint": ("tail", "-f", "/dev/null"),
        "network": NetworkPolicy.allow_all(),
        "resources": Resources("100m", "2", "512Mi", "4Gi"),
        "labels": Labels.of(user="u-1", kind="test"),
    }
    return CreateSpec(**(base | kw))


def test_labels_are_prefixed_and_checked_like_k8s_labels() -> None:
    labels = Labels.of(user="u-1", kind="sbx")
    assert labels.as_dict() == {"atlas.managed": "true", "atlas.user": "u-1", "atlas.kind": "sbx"}
    assert labels.with_sandbox("abc").as_dict()["atlas.sandbox"] == "abc"
    with pytest.raises(ValueError, match="≤63"):
        Labels.of(thread="x" * 64)
    with pytest.raises(ValueError, match="不合法"):
        Labels.of(user="a b")


def test_create_spec_requires_a_network_policy_and_distinct_env_names() -> None:
    with pytest.raises(TypeError, match="network 必填"):
        _spec(network=None)
    with pytest.raises(ValueError, match="重复"):
        _spec(env={"K": "1"}, secret_env={"K": SecretStr("2")})
    with pytest.raises(ValueError, match="重复"):
        _spec(env={"TOKEN": "1"}, service_token_env="TOKEN")
    with pytest.raises(ValueError, match="模板 code"):
        _spec(template="Bad_Code")


def test_policy_json_always_writes_the_default_action() -> None:
    """★ sidecar 把 networkPolicy: {} 当全拒绝，规范却写着全放行：默认动作必须显式。"""
    assert policy_json(NetworkPolicy.allow_all()) == {"networkPolicy": {"defaultAction": "allow"}}
    body = policy_json(NetworkPolicy.deny_all().allowing("pypi.org").with_credentials())
    assert body == {
        "networkPolicy": {
            "defaultAction": "deny",
            "egress": [{"action": "allow", "target": "pypi.org"}],
        },
        "credentialProxy": {"enabled": True},
    }
    assert NetworkPolicy.allow_all().trust() is None
    assert NetworkPolicy.allow_all().with_credentials().trust() is not None


def test_request_body_merges_secrets_and_both_tokens_into_env() -> None:
    spec = _spec(
        env={"HOME": "/home/agent"},
        secret_env={"MODEL_KEY": SecretStr("k")},
        service_token_env="BRIDGE_TOKEN",
        volumes=(Volume("workspace", "atlas-oss", "u/t/workspace", "/workspace"),),
    )
    body = request_body(spec, SecretStr("exec"), SecretStr("svc"))
    assert body["env"] == {
        "HOME": "/home/agent",
        "MODEL_KEY": "k",
        "EXECD_ACCESS_TOKEN": "exec",
        "BRIDGE_TOKEN": "svc",
    }
    assert body["timeout"] is None
    assert body["networkPolicy"] == {"defaultAction": "allow"}
    assert body["volumes"][0]["pvc"] == {"claimName": "atlas-oss", "createIfNotExists": False}


def test_state_groups_keep_unknown_states_out_of_every_group() -> None:
    assert SandboxState.parse("Allocated").usable and SandboxState.parse("Running").usable
    assert SandboxState.parse("Pending").settling
    assert SandboxState.parse("Terminated").leaving
    weird = SandboxState.parse("Hibernating")
    assert weird is SandboxState.UNKNOWN
    assert not (weird.usable or weird.settling or weird.leaving or weird.broken)


# ──────────────────────────────────────────────── 模板


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("      hostNetwork: true\n", "宿主命名空间"),
        ("      volumes:\n        - {name: h, hostPath: {path: /}}\n", "宿主目录"),
        ("      initContainers:\n        - name: x\n", "initContainers"),
    ],
)
def test_template_policy_rejects_escapes_to_the_node(change: str, match: str) -> None:
    with pytest.raises(TemplateRejected, match=match):
        TemplatePolicy().check(GOOD + change)


def test_template_policy_requires_one_sandbox_container_with_seccomp() -> None:
    TemplatePolicy().check(GOOD)
    with pytest.raises(TemplateRejected, match="sidecar"):
        TemplatePolicy().check(GOOD + "        - name: extra\n")
    with pytest.raises(TemplateRejected, match="特权"):
        TemplatePolicy().check(GOOD + "          securityContext: {privileged: true}\n")
    no_seccomp = "spec:\n  template:\n    spec:\n      containers:\n        - name: sandbox\n"
    with pytest.raises(TemplateRejected, match="seccomp"):
        TemplatePolicy().check(no_seccomp)


def test_seeded_templates_pass_the_policy() -> None:
    """迁移 0035 的种子（也是首次部署写 ConfigMap 的来源）本身得合规。"""
    from importlib import resources

    seed = resources.files("atlas_agent.sandbox.base.lifecycle").joinpath("seed")
    for code in ("sbx-strict", "cli", "bench"):
        TemplatePolicy().check(seed.joinpath(f"{code}.yaml").read_text(encoding="utf-8"))


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.live: dict[str, str] = {}

    async def publish(self, template: SandboxTemplate) -> None:
        self.published.append((template.server, template.yaml))
        self.live[template.server] = template.sha()

    async def live_sha(self, server: str) -> str | None:
        return self.live.get(server)


@pytest.fixture
async def scratch_templates() -> AsyncIterator[list[str]]:
    """用例自己建的模板 code：结束时删掉（种子模板不动）。"""
    codes: list[str] = []
    yield codes
    async with get_sessionmaker()() as session:
        await session.execute(delete(SandboxTemplate).where(SandboxTemplate.code.in_(codes)))
        await session.commit()


async def test_templates_are_revised_in_the_db_and_published_explicitly(
    scratch_templates: list[str],
) -> None:
    code = f"t-{uuid4().hex[:8]}"
    scratch_templates.append(code)
    publisher = FakePublisher()
    async with get_sessionmaker()() as session:
        templates = SandboxTemplates(session, publisher)
        created = await templates.create(code, "测试", f"srv-{code}", GOOD, by=ADMIN)
        assert (created.version, created.published_version) == (1, None)
        with pytest.raises(TemplateConflict):
            await templates.create(code, "", "other", GOOD, by=ADMIN)
        with pytest.raises(TemplateRejected):
            await templates.revise(code, GOOD + "      hostPID: true\n", by=ADMIN)
        assert (
            await templates.revise(code, GOOD + "      enableServiceLinks: false\n", by=ADMIN) == 2
        )
        assert publisher.published == []  # 修订只改数据库

        published = await templates.publish(code, by=ADMIN)
        assert published.published_version == 2
        assert published.published_sha == published.sha()
        assert await templates.drifted(code) is False
        publisher.live[f"srv-{code}"] = "edited-by-hand"
        assert await templates.drifted(code) is True
        await session.commit()


async def test_kube_publisher_writes_the_configmap_and_rolls_the_server() -> None:
    calls: list[tuple[str, str, Any]] = []
    rolled = {"done": False}

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            rolled["done"] = "deployments" in request.url.path
            return httpx.Response(200, json={})
        if "configmaps" in request.url.path:
            return httpx.Response(200, json={"data": {"sbx.yaml": GOOD}})
        ready = 1 if rolled["done"] else 0
        return httpx.Response(
            200,
            json={
                "metadata": {"generation": 2},
                "spec": {"replicas": 1},
                "status": {
                    "observedGeneration": 2,
                    "replicas": 1,
                    "updatedReplicas": ready,
                    "availableReplicas": ready,
                },
            },
        )

    api = KubeApi("https://k8s", "tok", transport=httpx.MockTransport(handle))
    publisher = KubeTemplatePublisher(api, poll_s=0.01)
    template = SandboxTemplate(code="sbx-strict", server="sbx", yaml=GOOD)
    await publisher.publish(template)
    cm, deploy = calls[0], calls[1]
    assert cm[:2] == (
        "PATCH",
        "/api/v1/namespaces/opensandbox-system/configmaps/opensandbox-sbx-template",
    )
    assert cm[2] == {"data": {"sbx.yaml": GOOD}}
    assert deploy[1] == "/apis/apps/v1/namespaces/opensandbox-system/deployments/opensandbox-sbx"
    annotations = deploy[2]["spec"]["template"]["metadata"]["annotations"]
    assert annotations == {SHA_ANNOTATION: template.sha()[:16]}
    assert await publisher.live_sha("sbx") == template.sha()


# ──────────────────────────────────────────────── 实例


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis() -> AsyncIterator[Any]:
    r = make_redis(get_settings())
    yield r
    await r.aclose()


@pytest.fixture
async def template() -> AsyncIterator[SandboxTemplate]:
    """一个已发布的模板，绑定到假 OpenSandbox 那个 server。"""
    code = f"t-{uuid4().hex[:8]}"
    row = SandboxTemplate(
        code=code,
        description="",
        server=f"srv-{code}",
        yaml=GOOD,
        published_version=3,
        published_sha="x",
        updated_by_uuid=ADMIN,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    yield row
    async with get_sessionmaker()() as session:
        await session.execute(delete(SandboxTemplate).where(SandboxTemplate.code == code))
        await session.execute(
            delete(SandboxInstance).where(SandboxInstance.template_uuid == row.template_uuid)
        )
        await session.commit()


def _runtime(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate, **kw: Any
) -> OpenSandboxRuntime:
    server = LifecycleServer(
        template.server, LIFECYCLE_URL, SecretStr("k"), transport=fake.async_transport
    )
    return OpenSandboxRuntime(
        {template.server: server}, get_sessionmaker(), redis, settle_poll_s=0.01, **kw
    )


async def _events(sandbox_uuid: UUID) -> list[str]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(SandboxEvent)
            .where(SandboxEvent.sandbox_uuid == sandbox_uuid)
            .order_by(SandboxEvent.id)
        )
        return [r.event_type for r in rows]


async def test_launch_registers_first_then_creates_with_the_sandbox_label(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    inst = await rt.launch(sid, _spec(template.code, service_token_env="BRIDGE_TOKEN"))
    [sb] = fake.created
    assert inst.external_id == sb.id and inst.usable()
    assert inst.template_version == 3  # 建时模板的已发布版本
    assert sb.metadata["atlas.sandbox"] == str(sid) and sb.metadata["atlas.managed"] == "true"
    # ★ 两个令牌分开：execd 令牌不出基座，交出去的只有服务令牌
    service = await rt.service_token(sid)
    assert service is not None and sb.spec["env"]["BRIDGE_TOKEN"] == service.get_secret_value()
    access = await rt.access(sid)
    assert access.token.get_secret_value() == sb.token != service.get_secret_value()
    assert access.url == f"http://{sb.id}.execd:44772"
    assert await _events(sid) == ["registered", "created"]


async def test_the_same_uuid_creates_only_once(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    fake.create_delay_s = 0.2
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    results = await asyncio.gather(*(rt.launch(sid, _spec(template.code)) for _ in range(3)))
    assert len(fake.created) == 1
    assert {r.external_id for r in results} == {fake.created[0].id}
    assert (await rt.launch(sid, _spec(template.code))).external_id == fake.created[0].id
    assert len(fake.created) == 1


async def test_a_crash_between_create_and_backfill_is_adopted_by_label(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    """验收（B2）：进程崩在「建好」和「回填」之间，下一次用同一个 uuid 只建一个。"""
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    original = rt._created

    async def crash(*_: Any) -> Any:
        raise RuntimeError("进程在回填之前崩了")

    rt._created = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rt.launch(sid, _spec(template.code))
    assert len(fake.created) == 1
    row = await rt.get(sid)
    assert row is not None and row.external_id is None  # 登记在，id 没回填

    rt._created = original  # type: ignore[method-assign]
    inst = await rt.launch(sid, _spec(template.code))
    assert len(fake.created) == 1, "按 atlas.sandbox 标签找回，不再多建"
    assert inst.external_id == fake.created[0].id


async def test_a_failed_create_leaves_neither_a_registration_nor_a_half_sandbox(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    fake.create_state = "Failed"
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    with pytest.raises(LifecycleError, match="没有起来"):
        await rt.launch(sid, _spec(template.code))
    assert await rt.get(sid) is None
    assert fake.sandboxes == {}
    assert await _events(sid) == ["registered", "launch_failed"]


async def test_an_unpublished_template_cannot_launch(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    rt = _runtime(fake, redis, template)
    with pytest.raises(TemplateUnavailable, match="不存在"):
        await rt.launch(uuid7(), _spec("no-such-template"))
    assert fake.created == []


async def test_refresh_records_only_state_changes_and_forgets_vanished_sandboxes(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    inst = await rt.launch(sid, _spec(template.code))
    await rt.get(sid)
    await rt.get(sid)
    assert await _events(sid) == ["registered", "created"]  # 状态没变：不记
    fake.sandboxes[inst.external_id or ""].state = "Failed"
    refreshed = await rt.get(sid)
    assert refreshed is not None and refreshed.sandbox_state is SandboxState.FAILED
    assert await _events(sid) == ["registered", "created", "state_changed"]

    fake.vanish(inst.external_id or "")
    assert await rt.get(sid) is None
    with pytest.raises(SandboxGone):
        await rt.access(sid)
    assert (await _events(sid))[-1] == "reconciled"


async def test_destroy_takes_a_reason_and_is_idempotent(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    inst = await rt.launch(sid, _spec(template.code))
    await rt.destroy(sid, reason="idle")
    await rt.destroy(sid, reason="idle")
    assert fake.deleted == [inst.external_id]
    assert await rt.get(sid) is None
    history = await rt.history(sid)
    assert [e.event_type for e in history] == ["registered", "created", "destroyed"]
    assert history[-1].reason == "idle"


async def test_reconcile_deletes_unregistered_sandboxes_including_legacy_ones(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    """验收（B2）：对账删掉没有登记的实例，存量沙箱（旧标签、没有 atlas.sandbox）也在其中。"""
    rt = _runtime(fake, redis, template)
    keep = await rt.launch(uuid7(), _spec(template.code))
    legacy = fake.add(
        {"metadata": {"atlas.managed": "true", "atlas.kind": "sbx", "atlas.thread": "t"}}
    )
    stray = fake.add({"metadata": {"atlas.managed": "true", "atlas.sandbox": str(uuid7())}})
    other = fake.add({"metadata": {"app": "not-ours"}})

    report = await rt.reconcile()
    assert sorted(report.removed_remote) == sorted([legacy.id, stray.id])
    assert set(fake.sandboxes) == {keep.external_id, other.id}
    async with get_sessionmaker()() as session:
        logged = await session.scalars(
            select(SandboxEvent).where(SandboxEvent.external_id.in_([legacy.id, stray.id]))
        )
        assert {e.reason for e in logged} == {"reconcile"}


async def test_reconcile_adopts_a_registration_whose_sandbox_was_built(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    clock = {"now": datetime.now(UTC)}
    rt = _runtime(fake, redis, template, clock=lambda: clock["now"], reconcile_grace_s=60)
    built, lost = uuid7(), uuid7()

    async def crash(*_: Any) -> Any:
        raise RuntimeError("崩")

    original = rt._created
    rt._created = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rt.launch(built, _spec(template.code))
    rt._created = original  # type: ignore[method-assign]
    async with get_sessionmaker()() as session:  # 登记了、根本没建出来的
        session.add(
            SandboxInstance.register(
                lost,
                template_uuid=template.template_uuid,
                template_version=3,
                access_token=SecretStr("a"),
                service_token=None,
            )
        )
        await session.commit()

    report = await rt.reconcile()
    assert report.adopted == [built]
    assert lost not in report.removed_registrations  # 还年轻：可能正在建
    clock["now"] += timedelta(minutes=5)
    report = await rt.reconcile()
    assert report.removed_registrations == [lost]
    assert (await rt.get(built)) is not None and await rt.get(lost) is None


async def test_purge_history_drops_old_events_only(
    fake: FakeOpenSandbox, redis: Any, template: SandboxTemplate
) -> None:
    rt = _runtime(fake, redis, template)
    sid = uuid7()
    await rt.launch(sid, _spec(template.code))
    assert await rt.purge_history(datetime.now(UTC) - timedelta(days=1)) == 0
    assert len(await rt.history(sid)) == 2
    assert await rt.purge_history(datetime.now(UTC) + timedelta(seconds=1)) >= 2
    assert await rt.history(sid) == []
