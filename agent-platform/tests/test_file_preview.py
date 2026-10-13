"""会话文件预览站点（doc/detail/file-preview.html §12 的测试清单）。

不碰数据库、不碰真 Redis：对象存储用 moto 的内存 S3，Redis 用一个只实现
get / set / expire 的替身，会话行用替身仓库。

最要紧的是两组：
  · 安全头 —— **每一个**响应（含 404 / 410 / 304 / 400）都带 CSP sandbox，
    且 sandbox 里没有 allow-same-origin。新标签直接打开时只剩这一道隔离。
  · 路径逃逸 —— 预览与下载共用 _to_key 这一道关卡，读不到别的会话。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from uuid import uuid4

import boto3
import pytest
from atlas_server.api.v1 import previews
from atlas_server.config import Settings, get_settings
from atlas_server.deps import get_redis
from atlas_server.providers.filesystem.oss import OssFilesystem, WorkspaceListing
from atlas_server.services import preview as preview_mod
from atlas_server.services.preview import PreviewService
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from moto import mock_aws

BUCKET = "atlas-test"
USER = "u-test"
TOKEN = "tok-abcdefghijklmnop"


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value
        if ex is not None:
            self.ttl[key] = ex

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def expire(self, key: str, seconds: int) -> None:
        self.ttl[key] = seconds


def _settings(**kw) -> Settings:
    return Settings(litellm_key="x", oss_bucket=BUCKET, **kw)  # type: ignore[call-arg]


@pytest.fixture
def sessions():
    """同一个桶里的两个会话：a 有一个小站点，b 有一个「秘密」。"""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        a = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-a")
        b = OssFilesystem(s3, bucket=BUCKET, user_id=USER, thread_id="t-b")
        a.write("/workspace/index.html", "<script src='assets/app.js'></script>")
        a.write("/workspace/assets/app.js", "console.log('hi')")
        a.write("/workspace/docs/index.html", "<p>docs</p>")
        a.write("/workspace/notes.md", "# 标题\n" + "x" * 5000)
        a.write("/workspace/中文 名.txt", "你好")
        a.write("/workspace/.hidden/secret.txt", "hidden")
        a.write("/workspace/report.pdf", "%PDF-1.4")
        b.write("/workspace/secret.txt", "B-SECRET")
        yield a, b


@pytest.fixture
def client(sessions, monkeypatch):
    a, _ = sessions

    async def fake_resolve(self, token: str):
        return WorkspaceListing(a) if token == TOKEN else None

    monkeypatch.setattr(PreviewService, "resolve", fake_resolve)
    app = FastAPI()
    settings = _settings()
    # 与 main.create_app 同一份 CORS 配置：验证它不会改掉预览的 ACAO: *
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["Range"],
    )
    app.include_router(previews.router, prefix="/v1")
    app.dependency_overrides[get_redis] = lambda: FakeRedis()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def _url(path: str, token: str = TOKEN) -> str:
    return f"/v1/previews/{token}/{path}"


def _assert_isolated(resp) -> None:
    csp = resp.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert "frame-ancestors" in csp
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-content-type-options"] == "nosniff"


# ──────────────────────────────────────────────── 站点语义


def test_serves_file_with_type(client) -> None:
    resp = client.get(_url("assets/app.js"))
    assert resp.status_code == 200
    assert resp.text == "console.log('hi')"
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers["etag"]
    _assert_isolated(resp)


@pytest.mark.parametrize(("path", "body"), [("", "<script"), ("docs/", "<p>docs")])
def test_directory_serves_index(client, path, body) -> None:
    resp = client.get(_url(path))
    assert resp.status_code == 200
    assert resp.text.startswith(body)


def test_markdown_is_plain_text(client) -> None:
    # 新标签直接打开 .md 显示原文，而不是被浏览器当附件下载
    resp = client.get(_url("notes.md"))
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"


def test_unicode_path(client) -> None:
    resp = client.get(_url("%E4%B8%AD%E6%96%87%20%E5%90%8D.txt"))
    assert resp.status_code == 200
    assert resp.text == "你好"


def test_range_returns_206(client) -> None:
    resp = client.get(_url("notes.md"), headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.headers["content-range"].startswith("bytes 0-9/")
    assert len(resp.content) == 10
    _assert_isolated(resp)


def test_if_none_match_returns_304(client) -> None:
    etag = client.get(_url("assets/app.js")).headers["etag"]
    resp = client.get(_url("assets/app.js"), headers={"If-None-Match": etag})
    assert resp.status_code == 304
    _assert_isolated(resp)


def test_pdf_drops_sandbox_only(client) -> None:
    # Chrome 的 PDF 查看器在 CSP sandbox 下不工作 —— 只对 PDF 去掉 sandbox
    resp = client.get(_url("report.pdf"))
    assert "sandbox" not in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"


# ──────────────────────────────────────────────── 错误也要隔离


def test_expired_token_is_410(client) -> None:
    resp = client.get(_url("index.html", token="nope"))
    assert resp.status_code == 410
    _assert_isolated(resp)


def test_missing_file_is_404(client) -> None:
    resp = client.get(_url("nope.js"))
    assert resp.status_code == 404
    _assert_isolated(resp)


def test_hidden_segments_are_404(client) -> None:
    resp = client.get(_url(".hidden/secret.txt"))
    assert resp.status_code == 404
    _assert_isolated(resp)


@pytest.mark.parametrize(
    ("path", "reaches_route"),
    [
        # 字面量 ../ 在客户端（浏览器同样如此）就被规范化掉，请求落不到预览路由上
        ("../t-b/workspace/secret.txt", False),
        ("../../../u-test/t-b/workspace/secret.txt", False),
        # 编码过的能到达路由，由 _to_key 这一道关卡挡住
        ("..%2Ft-b%2Fworkspace%2Fsecret.txt", True),
        ("%2e%2e/%2e%2e/t-b/workspace/secret.txt", True),
        ("%2e%2e%2f%2e%2e%2f%2e%2e%2fu-test%2ft-b%2fworkspace%2fsecret.txt", True),
        ("/workspace/../skills/x", True),
    ],
)
def test_path_escape_cannot_read_other_session(client, path, reaches_route) -> None:
    resp = client.get(_url(path))
    # 410：规范化后令牌那一段被吃掉，剩下的被当成一个无效令牌 —— 同样读不到
    assert resp.status_code in (400, 404, 410)
    assert "B-SECRET" not in resp.text
    if reaches_route:
        _assert_isolated(resp)


# ──────────────────────────────────────────────── CORS


def test_opaque_origin_gets_wildcard_cors(client) -> None:
    # sandbox 文档的 origin 是 null：模块脚本 / 字体 / fetch 都是跨域请求
    resp = client.get(_url("assets/app.js"), headers={"Origin": "null"})
    assert resp.headers["access-control-allow-origin"] == "*"


def test_platform_origin_can_read_content_range(client) -> None:
    resp = client.get(
        _url("notes.md"),
        headers={"Origin": "http://localhost:3000", "Range": "bytes=0-9"},
    )
    assert "Content-Range" in resp.headers["access-control-expose-headers"]


# ──────────────────────────────────────────────── 令牌


class _Repo:
    def __init__(self, thread) -> None:
        self.thread = thread

    async def get(self, thread_id):
        return (self.thread, None) if self.thread and thread_id == self.thread.id else None


async def test_issue_and_resolve_token(monkeypatch) -> None:
    redis = FakeRedis()
    settings = _settings(preview_token_ttl_s=120)
    parent = uuid4()
    thread = SimpleNamespace(id=uuid4(), created_by=uuid4(), workspace_thread_id=parent)
    service = PreviewService(redis, settings)  # type: ignore[arg-type]

    token, _ = await service.issue(_Repo(thread), thread.id)  # type: ignore[arg-type]
    assert len(token) >= 40

    seen = {}

    def fake_make_workspace(_settings, owner, thread_id, workspace_thread_id=None):
        seen.update(owner=owner, thread_id=thread_id, ws=workspace_thread_id)
        return SimpleNamespace()

    monkeypatch.setattr(preview_mod, "make_workspace", fake_make_workspace)
    monkeypatch.setattr(preview_mod, "WorkspaceListing", lambda fs: "listing")
    redis.ttl.clear()

    assert await service.resolve(token) == "listing"
    # 工作区按会话创建者构造；子会话解析到父会话的工作区
    assert seen == {"owner": thread.created_by, "thread_id": thread.id, "ws": parent}
    # 命中即续期
    assert redis.ttl == {f"atlas:preview:{token}": 120}
    assert await service.resolve("unknown") is None


async def test_issue_unknown_thread_is_404() -> None:
    from atlas_server.errors import NotFound

    service = PreviewService(FakeRedis(), _settings())  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await service.issue(_Repo(None), uuid4())  # type: ignore[arg-type]


def test_base_url_prefers_configured_origin() -> None:
    same = PreviewService(FakeRedis(), _settings())  # type: ignore[arg-type]
    assert same.base_url("t", request_base="http://api:8000/") == "http://api:8000/v1/previews/t/"
    split = PreviewService(  # type: ignore[arg-type]
        FakeRedis(), _settings(preview_base_url="https://preview.example.com/")
    )
    assert split.base_url("t", request_base="http://api:8000/") == (
        "https://preview.example.com/v1/previews/t/"
    )


# ──────────────────────────────────────────────── 其它


def test_listing_includes_etag(sessions) -> None:
    a, _ = sessions
    files, _ = WorkspaceListing(a).list()
    assert all(f["etag"] for f in files)


def test_access_log_masks_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s"',
        ("1.2.3.4", "GET", f"/v1/previews/{TOKEN}/index.html"),
        None,
    )
    previews._MaskPreviewTokens().filter(record)
    assert TOKEN not in record.getMessage()
    assert "/v1/previews/tok-ab…/index.html" in record.getMessage()
