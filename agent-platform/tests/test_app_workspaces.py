"""按权限主体分的工作空间与沙箱、应用工作空间（sandbox/design-workspace-subject.html）。

布局       应用工作空间在相对路径里是保留段 space/：对象存储 {应用}/{编号}/，沙箱 /space
沙箱       以数字员工为权限一棵会话树一个沙箱（原生不给共享键、会话 Pod 的键带根会话），挂 /space；
           配额按主体种类分开
接口       应用按编号申请（幂等）、读写文件、释放；只认登记它的应用
会话       建会话时定文件主体（数字员工）、挂空间；有使用权的人能看、能停；开新一轮只认建会话的
           应用；空间释放后开新一轮 410
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.models import Thread
from atlas_agent.db.session import fetch_one, get_sessionmaker
from atlas_agent.domain.workspace_layout import WorkspaceLayout, app_space_prefix
from atlas_agent.main import create_app
from atlas_agent.providers.filesystem import PathEscape, PathGate
from atlas_agent.runtime_config import RuntimeConfig, WorkspaceLimits
from atlas_agent.sandbox.native import (
    ImagePolicy,
    NativeRequest,
    NativeSandboxKind,
    WorkspaceRef,
)
from atlas_agent.sandbox.provisioning.model import (
    QuotaExceeded,
    QuotaPolicy,
    SandboxAccount,
    SandboxStatus,
    Subject,
    UserSandbox,
)
from atlas_engine.contracts.spec import SandboxSpec
from httpx import ASGITransport
from moto import mock_aws

from tests.fake_uc import FakeUC

BUCKET = "atlas-app-ws"
SPEC = {"system_prompt": "p", "model": {"model": "claude-opus-5"}, "tool_names": []}
TEXT = {"content": [{"type": "text", "text": "hi"}]}


# ──────────────────────────────────────────────── 布局与路径关卡（纯计算）


def _space_layout() -> WorkspaceLayout:
    return WorkspaceLayout.of("de", "t", "workspace", space=app_space_prefix("teamflow", "s1"))


def test_space_is_a_reserved_segment_mapped_to_its_own_prefix() -> None:
    layout = _space_layout()
    assert layout.workspace == "space" and layout.workdir == "/space"
    assert layout.atlas_dir == "space/.atlas/t"
    assert layout.key("space/编码/变更.patch") == "teamflow/s1/编码/变更.patch"
    assert layout.real("space/编码/变更.patch") == "/space/编码/变更.patch"
    # 主体前缀下的照旧（技能、home 都在数字员工名下）
    assert layout.key("skills/a/1/SKILL.md") == "de/skills/a/1/SKILL.md"
    assert layout.real("home/.claude") == "/data/home/.claude"
    # 反算
    assert layout.rel_of_key("teamflow/s1/编码/x") == "space/编码/x"
    assert layout.rel_of_key("de/skills/a/1/x") == "skills/a/1/x"
    assert layout.rel_of_real("/space/编码/x") == "space/编码/x"
    assert layout.rel_of_real("/space") == "space"
    assert layout.rel_of_real("/data/skills/a/1/x") == "skills/a/1/x"
    # 没用应用工作空间的会话：space 不是保留段，就是 files/ 下的普通名字
    plain = WorkspaceLayout.of("u", "t", "workspace")
    assert plain.key("space/x") == "u/space/x" and plain.real("space/x") == "/data/space/x"


def test_path_gate_confines_space_sessions_to_the_space_and_skills() -> None:
    gate = PathGate(_space_layout(), skills=[("a", 1)])
    assert gate.resolve("编码/变更.patch").rel == "space/编码/变更.patch"
    assert gate.resolve("/space/编码/变更.patch").rel == "space/编码/变更.patch"
    assert gate.resolve("/data/skills/a/1/SKILL.md").rel == "skills/a/1/SKILL.md"
    assert gate.shown("space/编码/变更.patch") == "编码/变更.patch"
    assert gate.shown("skills/a/1/SKILL.md") == "/data/skills/a/1/SKILL.md"
    # 数字员工自己的 files/ 不在这个会话能用的根里；越出空间的也不行
    for bad in ("/data/files/workspace/x", "/space/../files/x", "../x", "/space"):
        with pytest.raises(PathEscape):
            gate.resolve(bad)


# ──────────────────────────────────────────────── 沙箱：共享、挂载、配额


def _native() -> NativeSandboxKind:
    images = ImagePolicy("atlas-sandbox:dev", {}, bench_available=False)
    return NativeSandboxKind(get_settings(), None, images)  # type: ignore[arg-type]


def test_digital_employee_sessions_get_their_own_sandbox_with_the_space_mounted() -> None:
    kind = _native()
    person = WorkspaceRef(uuid4(), uuid4(), "workspace")
    agent = WorkspaceRef(
        uuid4(), uuid4(), "workspace", space=app_space_prefix("teamflow", "s1"), exclusive=True
    )
    assert kind.share(NativeRequest(SandboxSpec(), person)) is not None
    assert kind.share(NativeRequest(SandboxSpec(), agent)) is None
    spec = kind.build(NativeRequest(SandboxSpec(), agent), agent.biz())
    mounts = {v.mount_path: v.sub_path for v in spec.volumes}
    assert mounts == {"/data": str(agent.user_uuid), "/space": "teamflow/s1"}
    assert agent.biz().subject is Subject.AGENT
    # 按人共享的沙箱只挂 /data
    plain = kind.build(NativeRequest(SandboxSpec(), person), person.biz())
    assert [v.mount_path for v in plain.volumes] == ["/data"]


def test_cli_pods_of_a_digital_employee_are_shared_only_within_one_session_tree() -> None:
    from atlas_agent.sandbox.session_pod import CliPodKind, PodRequest
    from atlas_engine.contracts.spec import CliSpec

    kind = CliPodKind(get_settings(), None, None)  # type: ignore[arg-type]
    base = {
        "thread_uuid": uuid4(),
        "workspace_dir": "w",
        "user_uuid": uuid4(),
        "cli": CliSpec(cli_type="claude-code"),
    }
    tree_a, tree_b = uuid4(), uuid4()
    person = kind.share(PodRequest(**base))
    a = kind.share(PodRequest(**base, tree=tree_a, space="teamflow/s1"))
    b = kind.share(PodRequest(**base, tree=tree_b))
    assert len({person.key, a.key, b.key}) == 3
    req = PodRequest(**base, tree=tree_a, space="teamflow/s1")
    assert kind.share(req).key == a.key  # 同一棵树的子会话共用
    assert [v.mount_path for v in kind._volumes(req.layout)] == ["/data", "/space"]
    assert req.layout.workdir == "/space"


def test_quota_is_configured_per_subject_kind() -> None:
    account = SandboxAccount(user_uuid=uuid4())
    account.sandboxes = [
        UserSandbox(uuid4(), account.user_uuid, "t", "img", status=SandboxStatus.READY)
        for _ in range(3)
    ]
    quota = QuotaPolicy(3, agent_limit=10)
    with pytest.raises(QuotaExceeded):
        quota.admit(account)
    quota.admit(account, Subject.AGENT)  # 数字员工的上限另配
    with pytest.raises(QuotaExceeded):
        QuotaPolicy(3).admit(account, Subject.AGENT)  # 没另配就和人一样


# ──────────────────────────────────────────────── 接口（moto 内存桶 + 测试库）


class _NoopExecutor:
    async def dispatch(self, run_id: Any) -> None:
        return None

    async def cancel(self, run_id: Any) -> None:
        return None


@pytest.fixture
def oss(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setenv("OSS_BUCKET", BUCKET)
        get_settings.cache_clear()
        yield client
    get_settings.cache_clear()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(self: RuntimeConfig) -> WorkspaceLimits:
        return WorkspaceLimits(upload_max=10 * 1024 * 1024, quota=0)

    monkeypatch.setattr(RuntimeConfig, "workspace_limits", fake)


@pytest.fixture
async def app(oss: Any, limits: None, clean_db: None) -> AsyncIterator[Any]:
    app = create_app()
    app.state.executor = _NoopExecutor()
    yield app


def _client(app: Any, uid: str, caller: str = "gateway") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        headers={"X-Uid": uid, "X-Caller-App": caller},
    )


async def _agent(c: httpx.AsyncClient, *, auth_subject: str) -> str:
    r = await c.post(
        "/v1/agents",
        json={
            "slug": f"a-{uuid4().hex[:8]}",
            "name": "开发工程师",
            "spec": SPEC,
            "auth_subject": auth_subject,
        },
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert (await c.post(f"/v1/agents/{aid}/status", json={"status": "enabled"})).is_success
    return str(aid)


async def test_app_workspace_files_and_digital_employee_sessions(
    app: Any, oss: Any, fake_user_center: FakeUC
) -> None:
    fake = fake_user_center
    li = fake.add("li", ["chat:use", "agent:create"])
    zhou = fake.add("zhou", ["chat:use"])
    chen = fake.add("chen", ["chat:use"])
    space = str(uuid4())
    async with (
        _client(app, li, "teamflow") as tf_li,
        _client(app, zhou, "teamflow") as tf_zhou,
        _client(app, li) as gw_li,
        _client(app, zhou) as gw_zhou,
        _client(app, chen) as gw_chen,
        _client(app, li, "bench") as bench,
    ):
        de_agent = await _agent(gw_li, auth_subject="agent")
        personal = await _agent(gw_li, auth_subject="user")
        fake.share("AtlasAgent", de_agent, zhou, "READ")

        # ── 申请：按编号幂等；别的应用、网关来的用不了
        r = await tf_li.post("/v1/app-workspaces", json={"space_uuid": space, "title": "F-1001"})
        assert r.status_code == 201, r.text
        assert r.json()["app_code"] == "teamflow"
        again = await tf_li.post("/v1/app-workspaces", json={"space_uuid": space, "title": "x"})
        assert again.status_code == 200 and again.json()["title"] == "F-1001"
        assert (
            await bench.post("/v1/app-workspaces", json={"space_uuid": space})
        ).status_code == 409
        assert (await bench.get(f"/v1/app-workspaces/{space}/files")).status_code == 404
        assert (await gw_li.get(f"/v1/app-workspaces/{space}/files")).status_code == 404

        # ── 文件：直连对象存储，目录 teamflow/{编号}/
        r = await tf_li.put(
            f"/v1/app-workspaces/{space}/files", params={"path": "资料/现状.md"}, content=b"# x"
        )
        assert r.status_code == 201, r.text
        assert oss.get_object(Bucket=BUCKET, Key=f"teamflow/{space}/资料/现状.md")["Body"].read()
        root = (await tf_li.get(f"/v1/app-workspaces/{space}/files")).json()
        assert [d["path"] for d in root["dirs"]] == ["资料"]
        inner = (
            await tf_li.get(f"/v1/app-workspaces/{space}/files", params={"dir": "资料"})
        ).json()
        assert [f["path"] for f in inner["files"]] == ["资料/现状.md"]
        stat = await tf_li.post(
            f"/v1/app-workspaces/{space}/files/stat", json={"paths": ["资料/现状.md", "编码/x"]}
        )
        assert stat.json() == {"missing": ["编码/x"]}
        got = await tf_li.get(
            f"/v1/app-workspaces/{space}/files/download", params={"path": "资料/现状.md"}
        )
        assert got.status_code == 200 and got.content == b"# x"
        bad = await tf_li.put(
            f"/v1/app-workspaces/{space}/files", params={"path": "../x"}, content=b"x"
        )
        assert bad.status_code == 400

        # ── 建会话：只给以数字员工为权限的智能体，只有登记它的应用能指定
        body = {"agent_id": personal, "title": "编码", "app_workspace": space}
        assert (await tf_li.post("/v1/threads", json=body)).status_code == 409
        body["agent_id"] = de_agent
        assert (await gw_li.post("/v1/threads", json=body)).status_code == 404
        r = await tf_li.post("/v1/threads", json=body)
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        async with get_sessionmaker()() as s:
            thread = await fetch_one(s, Thread, Thread.thread_uuid == UUID(tid))
            assert thread is not None
            de = (await gw_li.get(f"/v1/agents/{de_agent}")).json()["digital_employee_uid"]
            assert str(thread.owner_uuid) == de and thread.owned_by_agent
            assert str(thread.created_by_uuid) == li
            assert thread.space_prefix == f"teamflow/{space}"

        # ── 看：有使用权的人从网关也能看（会话的文件就是空间里的），没有的看不到
        files = (await gw_zhou.get(f"/v1/threads/{tid}/files")).json()
        assert [f["path"] for f in files["data"]] == ["资料/现状.md"]
        assert (await gw_chen.get(f"/v1/threads/{tid}")).status_code == 404

        # ── 开新一轮只认 TeamFlow；停止前端直连
        assert (await gw_zhou.post(f"/v1/threads/{tid}/runs", json=TEXT)).status_code == 403
        r = await tf_zhou.post(f"/v1/threads/{tid}/runs", json=TEXT)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert (await gw_zhou.post(f"/v1/runs/{run_id}/cancel")).status_code == 200
        assert (await gw_chen.post(f"/v1/runs/{run_id}/cancel")).status_code == 404

        # ── 释放：删目录；之后开新一轮、读写都报已释放（不回落到别处）
        assert (await tf_li.delete(f"/v1/app-workspaces/{space}")).status_code == 204
        assert (await tf_li.delete(f"/v1/app-workspaces/{space}")).status_code == 204
        assert "Contents" not in oss.list_objects_v2(Bucket=BUCKET, Prefix=f"teamflow/{space}/")
        r = await tf_zhou.post(f"/v1/threads/{tid}/runs", json=TEXT)
        assert r.status_code == 410 and r.json()["error"]["kind"] == "space_released"
        assert (await tf_li.get(f"/v1/app-workspaces/{space}/files")).status_code == 410
        r = await tf_li.post("/v1/app-workspaces", json={"space_uuid": space})
        assert r.status_code == 410


async def test_personal_sessions_keep_files_under_the_creator(
    app: Any, fake_user_center: FakeUC
) -> None:
    """以人为权限：文件主体是创建人，别人用同一个智能体看不到。"""
    fake = fake_user_center
    li = fake.add("li", ["chat:use", "agent:create"])
    zhou = fake.add("zhou", ["chat:use"])
    async with _client(app, li) as gw_li, _client(app, zhou) as gw_zhou:
        aid = await _agent(gw_li, auth_subject="user")
        fake.share("AtlasAgent", aid, zhou, "READ")
        tid = (await gw_li.post("/v1/threads", json={"agent_id": aid})).json()["id"]
        async with get_sessionmaker()() as s:
            thread = await fetch_one(s, Thread, Thread.thread_uuid == UUID(tid))
            assert thread is not None
            assert str(thread.owner_uuid) == li and not thread.owned_by_agent
            assert thread.space_prefix is None
        assert (await gw_zhou.get(f"/v1/threads/{tid}")).status_code == 404
