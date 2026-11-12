"""工作区读写的两条路（c4-model/agent/code-workspace.html · StoreRouter；改造计划 B6）。

    没有运行中的沙箱   文件工具直连对象存储（moto）
    这个人的沙箱在跑   一律经沙箱内执行的文件会话走 Pod 里的挂载（/data）—— 与命令同一个挂载；
                       工作区、技能都在它下面

最重要的一组是对齐测试：同一串操作在两条路上必须给出一样的结果（列举语义、错误文案、隐藏文件、
递归删除），否则模型会因为「沙箱是否在跑」看到两个不同的文件系统。另外覆盖：命令看得见工具写的文件、
沙箱没了回退对象存储并去掉记录、沙箱不响应时读降级写报错、同步方法不走路由。
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from atlas_agent.config import get_settings
from atlas_agent.providers.filesystem import Workspace, WorkspaceRouting
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.exec import PollingPolicy, ShellSandbox
from atlas_agent.sandbox.native import WorkspaceRef
from atlas_engine.contracts.spec import SandboxSpec
from moto import mock_aws

from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import component, new_workspace, workspace_dir

BUCKET = "atlas-test"


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
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


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def c(fake: FakeOpenSandbox, redis: Any) -> SandboxComponent:
    return component(fake, redis)


def _fs(s3: Any, ws: WorkspaceRef, c: SandboxComponent | None) -> Workspace:
    routing = WorkspaceRouting(c.native, c.exec, ws) if c else None
    return Workspace(
        s3,
        bucket=BUCKET,
        user_id=ws.user_uuid,
        thread_id=ws.root_thread_uuid,
        workspace_dir=ws.workspace_dir,
        routing=routing,
    )


def _prefix(ws: WorkspaceRef) -> str:
    return f"{ws.user_uuid}/files/{ws.workspace_dir}/"


def _objects(s3: Any, prefix: str) -> list[str]:
    page = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return sorted(o["Key"] for o in page.get("Contents", ()))


def _norm(result: Any) -> dict[str, Any]:
    """结果对象 → 可比较的 dict（读结果里的时间戳每次都不同，去掉）。"""
    data = dataclasses.asdict(result)
    if isinstance(data.get("file_data"), dict):
        data["file_data"] = {
            k: v for k, v in data["file_data"].items() if k not in ("created_at", "modified_at")
        }
    return data


# ──────────────────────────────────────────────── 两条路对齐

_OPS: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("awrite", ("a.txt", "hello\nworld\n"), {}),
    ("awrite", ("dir/b.txt", "b"), {}),
    ("awrite", ("dir/sub/c.md", "c c c"), {}),
    ("awrite", ("dir/sub/d.md", "d"), {}),
    ("awrite", (".cache/x", "hidden"), {}),
    ("aread", ("a.txt",), {}),
    ("aread", ("a.txt",), {"offset": 1, "limit": 1}),
    ("aread", ("missing.txt",), {}),
    ("aread", ("../../a.txt",), {}),  # 越出工作区：两条路给出同一个错误
    ("aread", ("/",), {}),
    ("aedit", ("dir/sub/c.md", "c", "e"), {}),  # 出现多次：拒绝
    ("aedit", ("dir/sub/c.md", "c", "e"), {"replace_all": True}),
    ("aedit", ("a.txt", "world", "there"), {}),
    ("aedit", ("a.txt", "absent", "x"), {}),
    ("aedit", ("nope.txt", "a", "b"), {}),
    ("aread", ("dir/sub/c.md",), {}),
    ("aread", ("a.txt",), {}),
    ("asearch", ("",), {}),
    ("asearch", ("dir/",), {}),
    ("asearch", ("dir/sub/",), {}),
    ("asearch", ("dir/b",), {}),  # 停在半个文件名上
    ("asearch", ("",), {"pattern": "**/*.md"}),
    ("asearch", ("",), {"pattern": "**"}),
    ("asearch", ("",), {"pattern": "**", "max_keys": 2}),
    ("asearch", (".cache/",), {}),
    ("asearch", ("nothing-here/",), {}),
    ("adelete", ("dir/b.txt",), {}),
    ("adelete", ("dir/b.txt",), {}),
    ("adelete", ("dir",), {"recursive": True}),
    ("adelete", ("dir",), {"recursive": True}),
    ("asearch", ("",), {"pattern": "**"}),
]


async def test_routed_and_direct_paths_give_identical_results(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    direct = _fs(s3, new_workspace(), None)
    await c.native.ensure(ws, SandboxSpec())
    routed = _fs(s3, ws, c)
    for name, args, kwargs in _OPS:
        want = _norm(await getattr(direct, name)(*args, **kwargs))
        got = _norm(await getattr(routed, name)(*args, **kwargs))
        assert got == want, f"{name}{args}{kwargs}"
    assert _objects(s3, _prefix(ws)) == []  # 沙箱在跑时一个字节都没直接写进对象存储
    mount = workspace_dir(fake, ws)
    on_mount = sorted(f.relative_to(mount).as_posix() for f in mount.rglob("*") if f.is_file())
    assert on_mount == [".cache/x", "a.txt"]  # 都落在了 Pod 的挂载上


# ──────────────────────────────────────────────── 一致性：工具与命令看到同一份数据


async def test_commands_see_what_file_tools_wrote_and_vice_versa(
    s3: Any, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    sbx = ShellSandbox(
        native=c.native,
        exec=c.exec,
        ledger=c.ledger,
        ws=ws,
        run_id=ws.root_thread_uuid,
        spec=SandboxSpec(),
        polling=PollingPolicy(0.02, 0.05),
    )
    await sbx.aexecute("true")  # 沙箱起来 → 通用供给标为 READY
    fs = _fs(s3, ws, c)
    await fs.awrite("notes/plan.md", "step 1\n")
    assert (await sbx.aexecute("cat notes/plan.md")).output == "step 1\n"
    await sbx.aexecute("echo 'step 2' >> notes/plan.md; echo out > result.txt")
    assert (await fs.aread("notes/plan.md")).file_data["content"] == "step 1\nstep 2\n"
    assert (await fs.asearch("")).keys == ["result.txt"]


async def test_ui_upload_goes_through_the_running_sandbox(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """★ 界面上传和模型的写走同一个选路：沙箱在跑时经沙箱写，只有一个写入方。"""
    from atlas_agent.providers.filesystem import WorkspaceListing

    await c.native.ensure(ws, SandboxSpec())
    fs = _fs(s3, ws, c)
    assert await WorkspaceListing(fs).put("data/input.csv", b"a,b\n1,2\n") == 8
    assert (workspace_dir(fake, ws) / "data" / "input.csv").read_bytes() == b"a,b\n1,2\n"
    assert _objects(s3, _prefix(ws)) == []  # 没有绕过沙箱直写对象存储


async def test_without_a_running_sandbox_files_go_to_object_storage(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """★ 文件操作不建沙箱：多数会话从不跑命令，一直直连对象存储。"""
    fs = _fs(s3, ws, c)
    await fs.awrite("a.txt", "x")
    assert (await fs.aread("a.txt")).file_data["content"] == "x"
    assert _objects(s3, _prefix(ws)) == [f"{_prefix(ws)}a.txt"]
    assert fake.created == []


async def test_skills_are_read_through_the_same_mount(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """技能也在 /data 下：沙箱在跑时经沙箱读，和 Shell 看到的是同一份。"""
    await c.native.ensure(ws, SandboxSpec())
    skill = fake.workspace(f"{ws.user_uuid}/skills/pdf/1")
    (skill / "SKILL.md").write_text("# pdf")
    fs = Workspace(
        s3,
        bucket=BUCKET,
        user_id=ws.user_uuid,
        thread_id=ws.root_thread_uuid,
        workspace_dir=ws.workspace_dir,
        skills=[("pdf", 1)],
        routing=WorkspaceRouting(c.native, c.exec, ws),
    )
    assert (await fs.aread("/data/skills/pdf/1/SKILL.md")).file_data["content"] == "# pdf"
    assert (await fs.asearch("/data/skills/pdf/1/")).keys == ["/data/skills/pdf/1/SKILL.md"]
    assert _objects(s3, f"{ws.user_uuid}/") == []  # 没碰对象存储


async def test_another_session_of_the_same_person_routes_through_the_running_sandbox(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """★ 选路看这个人的沙箱：本会话没执行过命令（没绑沙箱），这个人的沙箱在跑，读写也经它 ——
    它挂着整个 {user}/，绕过它直写对象存储就成了第二个写入方。"""
    await c.native.ensure(ws, SandboxSpec())
    fresh = new_workspace()
    other = WorkspaceRef(fresh.root_thread_uuid, ws.user_uuid, fresh.workspace_dir)
    fs = _fs(s3, other, c)
    assert (await fs.awrite("a.txt", "x")).error is None
    assert _objects(s3, f"{ws.user_uuid}/") == []
    assert (workspace_dir(fake, other) / "a.txt").read_text() == "x"


async def test_sync_methods_never_route(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    await c.native.ensure(ws, SandboxSpec())
    fs = _fs(s3, ws, c)
    fs.write("a.txt", "x")
    assert _objects(s3, _prefix(ws)) == [f"{_prefix(ws)}a.txt"]
    assert list(workspace_dir(fake, ws).iterdir()) == []


# ──────────────────────────────────────────────── 沙箱出问题时


async def test_vanished_sandbox_falls_back_and_is_forgotten(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """Pod 没了：写穿挂载下文件早已在对象存储里，改走对象存储即可。"""
    s3.put_object(Bucket=BUCKET, Key=f"{_prefix(ws)}a.txt", Body=b"persisted")
    await c.native.ensure(ws, SandboxSpec())
    fake.vanish(fake.created[0].id)
    fs = _fs(s3, ws, c)
    assert (await fs.aread("a.txt")).file_data["content"] == "persisted"
    assert await c.native.current(ws) is None
    assert (await fs.awrite("b.txt", "y")).error is None  # 之后的写也直连对象存储
    assert f"{_prefix(ws)}b.txt" in _objects(s3, _prefix(ws))


async def test_unresponsive_sandbox_degrades_reads_and_refuses_writes(
    s3: Any, fake: FakeOpenSandbox, c: SandboxComponent, ws: WorkspaceRef
) -> None:
    """沙箱还在但 execd 不响应：读降级到对象存储。

    写不降级（那会多出一个写入方），报错让模型重试。
    """
    s3.put_object(Bucket=BUCKET, Key=f"{_prefix(ws)}a.txt", Body=b"persisted")
    ref = await c.native.ensure(ws, SandboxSpec())
    fs = _fs(s3, ws, c)
    fake.break_execd(100)
    assert (await fs.aread("a.txt")).file_data["content"] == "persisted"
    result = await fs.awrite("a.txt", "overwritten")
    assert result.error is not None and "暂时不可写" in result.error
    key = f"{_prefix(ws)}a.txt"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"persisted"
    assert await c.native.current(ws) == ref  # 不是沙箱没了：记录保留
