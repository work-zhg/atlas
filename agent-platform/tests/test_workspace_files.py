"""会话右侧「文件」面板：直接列工作区（而不是靠 file.written 事件拼）。

acp 子智能体的 CLI 在 Pod 里直接写挂载的工作区，平台侧不产生任何事件 ——
此前这类产物在面板上永远是空的（会话 065c767d… 就是这样）。
"""

from __future__ import annotations

from uuid import uuid4

import boto3
import pytest
from atlas_server.providers.filesystem.oss import OssFilesystem, PathEscape, WorkspaceListing
from moto import mock_aws

BUCKET = "ws-files"
USER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_lists_files_written_outside_the_platform(s3) -> None:  # type: ignore[no-untyped-def]
    parent = uuid4()
    fs = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id=parent)
    # 模拟 CLI 在 Pod 里直接写挂载的工作区：不经 OssFilesystem、没有任何事件
    ws = f"{USER}/{parent}/workspace"
    s3.put_object(Bucket=BUCKET, Key=f"{ws}/index.html", Body=b"<html>")
    s3.put_object(Bucket=BUCKET, Key=f"{ws}/assets/app.js", Body=b"x" * 10)
    s3.put_object(Bucket=BUCKET, Key=f"{ws}/.cache/tmp", Body=b"hidden")
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/{parent}/skills/a/SKILL.md", Body=b"skill")
    s3.put_object(Bucket=BUCKET, Key=f"{USER}/{parent}/system/x", Body=b"sys")

    files, truncated = WorkspaceListing(fs).list()
    assert [(f["path"], f["size"]) for f in files] == [("assets/app.js", 10), ("index.html", 6)]
    assert truncated is False
    assert all(f["modified_at"] for f in files)

    # 子会话共享父会话的工作区：看到的是同一份
    child = OssFilesystem(
        s3, bucket=BUCKET, user_id=USER, thread_id=uuid4(), workspace_thread_id=parent
    )
    assert [f["path"] for f in WorkspaceListing(child).list()[0]] == ["assets/app.js", "index.html"]


def test_truncates_long_listings(s3) -> None:  # type: ignore[no-untyped-def]
    fs = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id=uuid4())
    for i in range(5):
        fs.write(f"f{i}.txt", "x")
    files, truncated = WorkspaceListing(fs).list(max_keys=3)
    assert len(files) == 3 and truncated is True


def test_download_is_confined_to_the_workspace(s3) -> None:  # type: ignore[no-untyped-def]
    fs = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id=uuid4())
    fs.write("report.md", "# 报告")
    body, size = WorkspaceListing(fs).open("report.md")
    assert body.read().decode() == "# 报告" and size == len("# 报告".encode())
    # 挂载点前缀写法也认
    assert WorkspaceListing(fs).open("/workspace/report.md")[1] == size
    # 技能区不经这里下载
    with pytest.raises(PathEscape):
        WorkspaceListing(fs).open("/skills/a/SKILL.md")
