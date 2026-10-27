"""工作区读写的两条路（doc/sandbox-opensandbox-design.html §9、§10）。

    没有运行中的沙箱   文件工具直连对象存储（moto）
    沙箱在跑           工作区的 key 经 execd 文件 API 走 Pod 里的挂载 —— 与命令同一个挂载；
                       技能区仍走对象存储

最重要的一组是对齐测试：同一串操作在两条路上必须给出一样的结果（列举语义、错误文案、隐藏文件、
递归删除），否则模型会因为「沙箱是否在跑」看到两个不同的文件系统。另外覆盖：命令看得见工具写的文件、
沙箱没了回退对象存储并删路由、沙箱不响应时读降级写报错、同步方法不走路由。
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import boto3
import pytest
from atlas_agent.config import get_settings
from atlas_agent.providers.filesystem import WorkspaceRouting
from atlas_agent.providers.filesystem.oss import OssFilesystem
from atlas_agent.providers.opensandbox import SandboxProvisioner
from atlas_agent.providers.sandbox import PodSandbox
from atlas_agent.redisx import make_redis
from moto import mock_aws

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings, wipe

BUCKET = "atlas-test"
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


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def provisioner(fake: FakeOpenSandbox, redis: Any) -> SandboxProvisioner:
    return SandboxProvisioner(fake_settings(), redis, transport=fake.async_transport)


def _fs(
    s3: Any, ws: str, provisioner: SandboxProvisioner | None, fake: FakeOpenSandbox
) -> OssFilesystem:
    routing = (
        WorkspaceRouting(provisioner, ws, transport=fake.sync_transport) if provisioner else None
    )
    return OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id=ws, routing=routing)


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
    ("awrite", ("/workspace/dir/b.txt", "b"), {}),
    ("awrite", ("dir/sub/c.md", "c c c"), {}),
    ("awrite", ("dir/sub/d.md", "d"), {}),
    ("awrite", (".cache/x", "hidden"), {}),
    ("aread", ("a.txt",), {}),
    ("aread", ("/workspace/a.txt",), {"offset": 1, "limit": 1}),
    ("aread", ("missing.txt",), {}),
    ("aread", ("../../a.txt",), {}),  # 逃逸被归一回工作区
    ("aread", ("/workspace",), {}),
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
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    direct = _fs(s3, f"{ws}-direct", None, fake)
    await provisioner.ensure(ws, user=USER)
    routed = _fs(s3, ws, provisioner, fake)
    for name, args, kwargs in _OPS:
        want = _norm(await getattr(direct, name)(*args, **kwargs))
        got = _norm(await getattr(routed, name)(*args, **kwargs))
        assert got == want, f"{name}{args}{kwargs}"
    assert _objects(s3, f"{USER}/{ws}/") == []  # 沙箱在跑时一个字节都没直接写进对象存储
    mount = fake.workspace(ws)
    on_mount = sorted(f.relative_to(mount).as_posix() for f in mount.rglob("*") if f.is_file())
    assert on_mount == [".cache/x", "a.txt"]  # 都落在了 Pod 的挂载上


# ──────────────────────────────────────────────── 一致性：工具与命令看到同一份数据


async def test_commands_see_what_file_tools_wrote_and_vice_versa(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    sbx = PodSandbox(
        provisioner=provisioner,
        workspace_thread_id=ws,
        user_id=USER,
        run_id=f"run-{ws}",
        poll_min_s=0.02,
        poll_max_s=0.05,
        transport=fake.async_transport,
    )
    await sbx.aexecute("true")  # 沙箱起来 → 路由表有记录
    fs = _fs(s3, ws, provisioner, fake)
    await fs.awrite("notes/plan.md", "step 1\n")
    assert (await sbx.aexecute("cat notes/plan.md")).output == "step 1\n"
    await sbx.aexecute("echo 'step 2' >> notes/plan.md; echo out > result.txt")
    assert (await fs.aread("notes/plan.md")).file_data["content"] == "step 1\nstep 2\n"
    assert (await fs.asearch("")).keys == ["result.txt"]


async def test_without_a_running_sandbox_files_go_to_object_storage(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    """★ 文件操作不建沙箱：多数会话从不跑命令，一直直连对象存储。"""
    fs = _fs(s3, ws, provisioner, fake)
    await fs.awrite("a.txt", "x")
    assert (await fs.aread("a.txt")).file_data["content"] == "x"
    assert _objects(s3, f"{USER}/{ws}/") == [f"{USER}/{ws}/workspace/a.txt"]
    assert fake.created == []


async def test_skills_always_come_from_object_storage(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/{ws}/skills/pdf/SKILL.md", Body=b"# pdf")
    await provisioner.ensure(ws, user=USER)
    fs = _fs(s3, ws, provisioner, fake)
    assert (await fs.aread("/skills/pdf/SKILL.md")).file_data["content"] == "# pdf"
    assert (await fs.asearch("/skills/")).common_prefixes == ["skills/pdf/"]
    assert list(fake.workspace(ws).iterdir()) == []


async def test_sync_methods_never_route(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    await provisioner.ensure(ws, user=USER)
    fs = _fs(s3, ws, provisioner, fake)
    fs.write("a.txt", "x")
    assert _objects(s3, f"{USER}/{ws}/") == [f"{USER}/{ws}/workspace/a.txt"]
    assert list(fake.workspace(ws).iterdir()) == []


# ──────────────────────────────────────────────── 沙箱出问题时


async def test_vanished_sandbox_falls_back_and_drops_the_route(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    """Pod 没了：写穿挂载下文件早已在对象存储里，改走对象存储即可。"""
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/{ws}/workspace/a.txt", Body=b"persisted")
    ep = await provisioner.ensure(ws, user=USER)
    fake.vanish(ep.sandbox_id)
    fs = _fs(s3, ws, provisioner, fake)
    assert (await fs.aread("a.txt")).file_data["content"] == "persisted"
    assert await provisioner.routes.get(ws) is None
    assert (await fs.awrite("b.txt", "y")).error is None  # 之后的写也直连对象存储
    assert f"{USER}/{ws}/workspace/b.txt" in _objects(s3, f"{USER}/{ws}/")


async def test_unresponsive_sandbox_degrades_reads_and_refuses_writes(
    s3: Any, fake: FakeOpenSandbox, provisioner: SandboxProvisioner, ws: str
) -> None:
    """沙箱还在但 execd 不响应：读降级到对象存储；写不降级（那会多出一个写入方），报错让模型重试。

    ★ 写不降级的原因：沙箱里的进程还可能在写同一个文件。
    """
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/{ws}/workspace/a.txt", Body=b"persisted")
    ep = await provisioner.ensure(ws, user=USER)
    fs = _fs(s3, ws, provisioner, fake)
    fake.break_execd(100)
    assert (await fs.aread("a.txt")).file_data["content"] == "persisted"
    result = await fs.awrite("a.txt", "overwritten")
    assert result.error is not None and "暂时不可写" in result.error
    assert (
        s3.get_object(Bucket=BUCKET, Key=f"{USER}/{ws}/workspace/a.txt")["Body"].read()
        == b"persisted"
    )
    assert await provisioner.routes.get(ws) == ep  # 不是沙箱没了：路由保留
