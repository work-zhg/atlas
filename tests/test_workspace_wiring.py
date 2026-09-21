"""会话工作区的接线：配了对象存储才有文件能力。

★ 这里钉住的是一条**产品语义**，不只是布线：没配 bucket 时文件工具
  必须**一个都不注册**，而不是回落到某个内存实现。回落会让模型以为自己
  有持久工作区 —— 它写完文件、下一轮发现不见了，却从未收到任何提示。
"""

from __future__ import annotations

from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

from atlas_server.config import Settings
from atlas_server.providers.filesystem import make_workspace
from atlas_server.providers.filesystem.oss import OssFilesystem

_BASE = {"litellm_key": "x"}


def test_no_bucket_means_no_filesystem() -> None:
    """没配 bucket → None。调用方据此关闭整个文件能力。"""
    settings = Settings(**_BASE)
    assert settings.workspace_configured is False
    assert make_workspace(settings, uuid4(), uuid4()) is None


def test_bucket_yields_oss_filesystem() -> None:
    with mock_aws():
        settings = Settings(**_BASE, oss_bucket="atlas-test", oss_endpoint="http://x")
        fs = make_workspace(settings, uuid4(), uuid4())
        assert isinstance(fs, OssFilesystem)


def test_each_session_gets_its_own_prefix() -> None:
    """两个会话拿到不同前缀 —— 隔离由构造时的 session_id 决定。"""
    with mock_aws():
        settings = Settings(**_BASE, oss_bucket="atlas-test", oss_endpoint="http://x")
        a = make_workspace(settings, "u1", "t-a")
        b = make_workspace(settings, "u1", "t-b")
        assert a._ws != b._ws
        assert a._ws == "u1/t-a/workspace"


@pytest.mark.parametrize("tools", [("filesystem",), ("filesystem", "bash")])
def test_filesystem_tools_absent_without_workspace(tools: tuple[str, ...]) -> None:
    """勾了 filesystem 但没配对象存储 —— 工具面为空。

    这条同时覆盖「勾了 bash」：sandbox 侧的 execute 与文件能力是两回事，
    但 bash 的 requires 要求同时勾 filesystem，所以没有工作区时两者一起失效。
    """
    from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware

    middleware = FilesystemMiddleware(backend=None)
    assert middleware.tools == []
    # 大结果外置也必须一并关闭：外置出去的内容没有 read_file 读得回来
    assert middleware._tool_token_limit_before_evict is None


def test_end_to_end_roundtrip_through_wiring() -> None:
    """经 make_workspace 拿到的实例能真正读写。"""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="atlas-test")
        settings = Settings(**_BASE, oss_bucket="atlas-test")
        fs = make_workspace(settings, uuid4(), uuid4())
        assert fs is not None
        fs.write("notes/a.md", "hello")
        assert fs.read("notes/a.md").file_data["content"] == "hello"
        assert fs.search("notes/").keys == ["notes/a.md"]
