"""工作空间：一个人的全部文件（{user}/files/）与会话的工作目录。

工作目录   建会话时定下、落表（thread.workspace_dir）：指定了用指定的，没指定是 {日期}/{会话 id}；
           子会话用根会话的；不能是根目录、隐藏目录、不能越界
文件       列一层（文件夹的文件数、大小）、搜索、上传、建文件夹、改名、删除、打包、占用
会话       有会话在用的目录不能改名；正在运行的会话的工作目录不能删
上限       单个文件、存储配额读运行期配置
旧布局     {user}/{会话 id}/… 一次性删掉，files/、skills/、home/ 不动
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
from atlas_agent.config import Settings, get_settings
from atlas_agent.db.models import Run, Thread
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.domain.workspace_layout import (
    InvalidWorkspaceDir,
    WorkspaceLayout,
    default_workspace_dir,
    normalize_workspace_dir,
)
from atlas_agent.main import create_app
from atlas_agent.providers.filesystem import PathEscape, UserFiles, make_user_files
from atlas_agent.providers.filesystem.legacy import purge_legacy_workspaces
from atlas_agent.runtime_config import RuntimeConfig, RuntimeKeys, WorkspaceLimits
from httpx import ASGITransport
from moto import mock_aws
from sqlalchemy import select

from tests.fake_uc import AS_USER, TEST_USER

BUCKET = "atlas-test"
USER = "u-test"


# ──────────────────────────────────────────────── 工作目录（纯计算）


def test_default_dir_is_date_then_thread_id() -> None:
    tid = uuid4()
    assert default_workspace_dir(tid, date(2026, 11, 12)) == f"2026-11-12/{tid}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Q3 销售分析", "Q3 销售分析"),
        ("/Q3 销售分析/", "Q3 销售分析"),
        ("a//b/./c", "a/b/c"),
        ("前端原型/登录页", "前端原型/登录页"),
    ],
)
def test_user_given_dirs_are_normalized(raw: str, expected: str) -> None:
    assert normalize_workspace_dir(raw) == expected


@pytest.mark.parametrize(
    "bad", ["", "/", " ", ".", "../x", "a/../../b", ".atlas", "a/.hidden", "a\\b", "a\x00"]
)
def test_bad_dirs_are_rejected(bad: str) -> None:
    """根目录（= 全部文件交给智能体）、越界、隐藏目录都不行。"""
    with pytest.raises(InvalidWorkspaceDir):
        normalize_workspace_dir(bad)


def test_layout_puts_the_workspace_under_files() -> None:
    layout = WorkspaceLayout.of("u", "t", "Q3 分析/report")
    assert layout.workspace == "files/Q3 分析/report"
    assert layout.workdir == "/data/files/Q3 分析/report"
    assert layout.atlas_dir == "files/Q3 分析/report/.atlas/t"
    assert layout.key(layout.workspace) == "u/files/Q3 分析/report"
    assert WorkspaceLayout.of("u", "t", "").workspace == "files"  # 工作空间页面用的根


def test_workspace_limits_are_runtime_config_keys() -> None:
    keys = RuntimeKeys.of(
        Settings(litellm_key="x", workspace_upload_max_mb=7, workspace_quota_mb=0)
    )  # type: ignore[call-arg]
    names = {k.name: k.default for k in keys.all()}
    assert names["workspace.upload_max_mb"] == 7
    assert names["workspace.quota_mb"] == 0


# ──────────────────────────────────────────────── 文件（moto 内存桶）


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def files(s3: Any) -> UserFiles:
    settings = Settings(litellm_key="x", oss_bucket=BUCKET)  # type: ignore[call-arg]
    uf = make_user_files(settings, USER)
    assert uf is not None
    return uf


def _put(s3: Any, rel: str, body: bytes = b"x") -> None:
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/files/{rel}", Body=body)


def test_list_dir_aggregates_one_level(s3: Any, files: UserFiles) -> None:
    _put(s3, "Q3/report/summary.md", b"12345")
    _put(s3, "Q3/analysis.py", b"123")
    _put(s3, "Q3/.atlas/t/x.txt", b"hidden")
    _put(s3, "年终汇报.pptx", b"ppt")
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/files/空文件夹/", Body=b"")
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/skills/a/1/SKILL.md", Body=b"skill")

    root = files.list_dir("")
    assert [(d["name"], d["files"], d["size"]) for d in root["dirs"]] == [
        ("Q3", 2, 8),
        ("空文件夹", 0, 0),
    ]
    assert [f["name"] for f in root["files"]] == ["年终汇报.pptx"]  # skills/ 不在工作空间里

    q3 = files.list_dir("Q3")
    assert [d["path"] for d in q3["dirs"]] == ["Q3/report"]
    assert [f["path"] for f in q3["files"]] == ["Q3/analysis.py"]
    # 隐藏的要显式要
    assert [d["name"] for d in files.list_dir("Q3", hidden=True)["dirs"]] == [".atlas", "report"]


def test_paths_cannot_escape_the_file_space(files: UserFiles) -> None:
    for bad in ("../skills", "../../other-user/files", "a/../../x"):
        with pytest.raises(PathEscape):
            files.list_dir(bad)


async def test_upload_mkdir_move_delete(s3: Any, files: UserFiles) -> None:
    assert await files.put("报告/a.md", b"# a") == 3
    assert files.mkdir("报告/图") == "报告/图"
    assert files.is_dir("报告/图") and files.is_file("报告/a.md")

    # 文件改名
    assert files.move("报告/a.md", "报告/b.md") == "报告/b.md"
    assert not files.is_file("报告/a.md") and files.is_file("报告/b.md")
    # 文件夹改名：连同目录标记对象一起搬
    assert files.move("报告", "汇报") == "汇报"
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=USER)["Contents"])
    assert keys == [f"{USER}/files/汇报/b.md", f"{USER}/files/汇报/图/"]
    # 目标已存在不覆盖；不能移进自己
    files.mkdir("另一个")
    with pytest.raises(FileExistsError):
        files.move("汇报", "另一个")
    with pytest.raises(PathEscape):
        files.move("汇报", "汇报/里面")

    await files.delete("汇报")
    assert not files.is_dir("汇报")
    with pytest.raises(FileNotFoundError):
        await files.delete("汇报")


async def test_hidden_paths_are_read_only(s3: Any, files: UserFiles) -> None:
    """.atlas 是会话的内部记录：能读，不能经工作空间写、改名、删。"""
    _put(s3, "Q3/.atlas/t/x.txt", b"x")
    with pytest.raises(PathEscape):
        await files.put("Q3/.atlas/t/y.txt", b"y")
    with pytest.raises(PathEscape):
        await files.delete("Q3/.atlas")
    with pytest.raises(PathEscape):
        files.move("Q3/.atlas", "Q3/atlas")
    with pytest.raises(PathEscape):
        files.mkdir(".secret")
    body, _ = files.open("Q3/.atlas/t/x.txt")
    assert body.read() == b"x"


def test_search_usage_and_zip(s3: Any, files: UserFiles) -> None:
    _put(s3, "Q3 报告/summary.md", b"12345")
    _put(s3, "Q3 报告/data/sales.csv", b"a,b")
    _put(s3, "其他/report.md", b"r")
    _put(s3, "其他/.atlas/t/report-hidden.md", b"h")

    found = files.search("report")
    assert [f["path"] for f in found["files"]] == ["其他/report.md"]
    assert [d["path"] for d in files.search("q3")["dirs"]] == ["Q3 报告"]

    assert files.usage() == (5 + 3 + 1 + 1, 4)  # 隐藏的也算占用

    data = b"".join(files.zip("Q3 报告"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["data/sales.csv", "summary.md"]
        assert zf.read("summary.md") == b"12345"


def test_purge_deletes_only_the_old_layout(s3: Any) -> None:
    user, thread = str(uuid4()), str(uuid4())
    keep = [
        f"{user}/files/Q3/a.md",
        f"{user}/skills/x/1/SKILL.md",
        f"{user}/home/.claude/settings.json",
        "_skills/x/1/SKILL.md",
        f"not-a-user/{thread}/workspace/a.md",
    ]
    gone = [f"{user}/{thread}/workspace/a.md", f"{user}/{thread}/system/cli-home/x"]
    for k in keep + gone:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")

    dry = purge_legacy_workspaces(s3, BUCKET, dry_run=True)
    assert [(p.prefix, p.objects) for p in dry] == [(f"{user}/{thread}/", 2)]
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == len(keep) + len(gone)

    purge_legacy_workspaces(s3, BUCKET, dry_run=False)
    left = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"])
    assert left == sorted(keep)


# ──────────────────────────────────────────────── 接口（moto 内存桶 + 测试库）


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
def limits(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """运行期配置换成可改的值，不写测试库里的配置表。"""
    current = {"upload_max": 10 * 1024 * 1024, "quota": 0}

    async def fake(self: RuntimeConfig) -> WorkspaceLimits:
        return WorkspaceLimits(**current)

    monkeypatch.setattr(RuntimeConfig, "workspace_limits", fake)
    return current


@pytest.fixture
async def client(
    oss: Any, limits: dict[str, int], clean_db: None
) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AS_USER) as c:
        yield c


async def _agent(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/v1/agents",
        json={
            "slug": f"ws-{uuid4().hex[:8]}",
            "name": "工作空间",
            "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_thread_records_its_workspace_dir_at_creation(client: httpx.AsyncClient) -> None:
    agent = await _agent(client)
    given = await client.post("/v1/threads", json={"agent_id": agent, "workspace_dir": "/Q3 分析/"})
    assert given.status_code == 201, given.text
    assert given.json()["workspace_dir"] == "Q3 分析"

    auto = (await client.post("/v1/threads", json={"agent_id": agent})).json()
    assert auto["workspace_dir"] == f"{date.today().isoformat()}/{auto['id']}"

    bad = await client.post("/v1/threads", json={"agent_id": agent, "workspace_dir": "../x"})
    assert bad.status_code == 400

    # 落了表：再读一次还是它
    again = await client.get(f"/v1/threads/{given.json()['id']}")
    assert again.json()["workspace_dir"] == "Q3 分析"


async def test_subagent_threads_copy_the_parents_dir(client: httpx.AsyncClient) -> None:
    from atlas_agent.repositories.thread import ThreadRepository

    agent = await _agent(client)
    root = (
        await client.post("/v1/threads", json={"agent_id": agent, "workspace_dir": "Q3"})
    ).json()
    async with get_sessionmaker()() as session:
        child = await ThreadRepository(session).create(
            agent_id=UUID(agent),
            title="",
            created_by=UUID(TEST_USER),
            parent_thread_id=UUID(root["id"]),
            subagent_name="helper",
            workspace_dir="别的目录",  # 子会话一律用父会话的
        )
        await session.commit()
        assert child.workspace_dir == "Q3"


async def test_workspace_api_roundtrip(client: httpx.AsyncClient, oss: Any) -> None:
    put = await client.put("/v1/workspace/files", params={"path": "Q3/a.md"}, content=b"# a")
    assert put.status_code == 201, put.text
    assert (await client.post("/v1/workspace/folders", json={"path": "Q3/图"})).status_code == 201

    root = (await client.get("/v1/workspace/entries")).json()
    assert [(d["name"], d["files"]) for d in root["dirs"]] == [("Q3", 1)]
    q3 = (await client.get("/v1/workspace/entries", params={"dir": "Q3"})).json()
    assert [d["name"] for d in q3["dirs"]] == ["图"] and [f["name"] for f in q3["files"]] == [
        "a.md"
    ]

    dl = await client.get("/v1/workspace/files/download", params={"path": "Q3/a.md"})
    assert dl.content == b"# a"
    z = await client.get("/v1/workspace/zip", params={"path": "Q3"})
    assert zipfile.ZipFile(io.BytesIO(z.content)).namelist() == ["a.md"]

    overview = (await client.get("/v1/workspace")).json()
    assert overview["configured"] and overview["used_bytes"] == 3 and overview["files"] == 1

    moved = await client.post("/v1/workspace/move", json={"src": "Q3/a.md", "dst": "Q3/b.md"})
    assert moved.json()["path"] == "Q3/b.md"
    assert (await client.delete("/v1/workspace/entries", params={"path": "Q3"})).status_code == 204
    assert (await client.get("/v1/workspace/entries")).json()["dirs"] == []
    # 写进的是这个人的 files/ 前缀
    await client.put("/v1/workspace/files", params={"path": "x.md"}, content=b"x")
    keys = [o["Key"] for o in oss.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == [f"{TEST_USER}/files/x.md"]
    assert (await client.post("/v1/workspace/preview")).json()["base_url"].endswith("/")


async def test_dirs_used_by_threads_cannot_be_renamed(client: httpx.AsyncClient) -> None:
    """工作目录定了不能换：有会话在用的目录（或它的上级）不能改名、移动。"""
    agent = await _agent(client)
    await client.put("/v1/workspace/files", params={"path": "项目/Q3/a.md"}, content=b"a")
    t = (
        await client.post(
            "/v1/threads", json={"agent_id": agent, "workspace_dir": "项目/Q3", "title": "Q3 分析"}
        )
    ).json()

    refs = (await client.get("/v1/workspace/refs", params={"path": "项目"})).json()
    assert [(r["id"], r["running"]) for r in refs["threads"]] == [(t["id"], False)]

    for src in ("项目/Q3", "项目"):
        resp = await client.post("/v1/workspace/move", json={"src": src, "dst": "改名了"})
        assert resp.status_code == 409, resp.text
        assert "Q3 分析" in resp.json()["error"]["message"]
    # 工作目录里面的文件照常能改名
    ok = await client.post(
        "/v1/workspace/move", json={"src": "项目/Q3/a.md", "dst": "项目/Q3/b.md"}
    )
    assert ok.status_code == 200
    # 没在运行：可以删，会话保留
    assert (
        await client.delete("/v1/workspace/entries", params={"path": "项目"})
    ).status_code == 204
    assert (await client.get(f"/v1/threads/{t['id']}")).status_code == 200


async def test_running_threads_dirs_cannot_be_deleted(client: httpx.AsyncClient) -> None:
    agent = await _agent(client)
    await client.put("/v1/workspace/files", params={"path": "Q3/a.md"}, content=b"a")
    t = (await client.post("/v1/threads", json={"agent_id": agent, "workspace_dir": "Q3"})).json()
    async with get_sessionmaker()() as session:
        thread = (
            await session.execute(select(Thread).where(Thread.thread_uuid == UUID(t["id"])))
        ).scalar_one()
        session.add(
            Run(thread_uuid=thread.thread_uuid, agent_version_uuid=uuid4(), status="running")
        )
        await session.commit()

    refs = (await client.get("/v1/workspace/refs", params={"path": "Q3"})).json()
    assert refs["threads"][0]["running"] is True
    resp = await client.delete("/v1/workspace/entries", params={"path": "Q3"})
    assert resp.status_code == 409
    # 里面的单个文件可以删（界面上会提醒）
    assert (
        await client.delete("/v1/workspace/entries", params={"path": "Q3/a.md"})
    ).status_code == 204


async def test_upload_limits_come_from_runtime_config(
    client: httpx.AsyncClient, limits: dict[str, int]
) -> None:
    limits["upload_max"] = 4
    too_big = await client.put("/v1/workspace/files", params={"path": "a.bin"}, content=b"12345")
    assert too_big.status_code == 413

    limits.update(upload_max=100, quota=6)
    assert (
        await client.put("/v1/workspace/files", params={"path": "a.bin"}, content=b"1234")
    ).status_code == 201
    over = await client.put("/v1/workspace/files", params={"path": "b.bin"}, content=b"123")
    assert over.status_code == 413 and "配额" in over.json()["error"]["message"]

    # 对话里添加文件也按同一套
    agent = await _agent(client)
    t = (await client.post("/v1/threads", json={"agent_id": agent, "workspace_dir": "对话"})).json()
    resp = await client.put(
        f"/v1/threads/{t['id']}/files", params={"path": "c.bin"}, content=b"123"
    )
    assert resp.status_code == 413
