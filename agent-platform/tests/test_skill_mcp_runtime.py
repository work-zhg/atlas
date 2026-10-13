"""运行时对技能 / MCP 的消费（doc/skill-mcp-backend-design.html §7–§11）。

★ 不用 clean_db：开发库与测试共用，clean_db 会清掉开发者的 agent。这里需要
  写库的用例都在一个**最后回滚**的事务里做。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_config.schemas import SkillCatalogItem, SkillVersionBrief, SkillVersionOut
from atlas_server.config import Settings, get_settings
from atlas_server.configplane import (
    ConfigPlaneUnavailable,
    FakeSkillDirectory,
    HttpSkillDirectory,
    UnconfiguredSkillDirectory,
    override_skill_directory,
)
from atlas_server.configplane.client import ConfigClient
from atlas_server.configplane.skills import SkillNotFound
from atlas_server.domain.events import EventType
from atlas_server.domain.skill_events import SkillLoadTracker, skill_slug_of
from atlas_server.domain.spec import AgentSpec, ModelSpec, SkillRefSpec
from atlas_server.errors import InvalidReference
from atlas_server.providers.filesystem.skill_copy import SkillUnavailable, resolve_skills
from atlas_server.providers.mcp import CallContext, McpServerConfig, McpUnavailable, NativeMcpTools
from atlas_server.providers.mcp.acp import acp_mcp_servers
from atlas_server.providers.mcp.catalog import (
    McpToolDef,
    ServerSnapshot,
    SettingsCatalog,
    _carry_history,
    build_snapshot,
)
from atlas_server.redisx import make_redis
from atlas_server.schemas.agent import AgentSpecIn, SkillRefIn, SubAgentSpecIn
from atlas_server.services.spec_refs import resolve_spec_refs


def _settings(**kw: Any) -> Settings:
    return Settings(litellm_key="x", **kw)  # type: ignore[call-arg]


@pytest.fixture
def fake_skills() -> Iterator[FakeSkillDirectory]:
    fake = FakeSkillDirectory()
    override_skill_directory(fake)
    yield fake
    override_skill_directory(None)


# ═══════════════════════════════════════════════ skill.loaded 的识别


def _started(call_id: str, path: str, name: str = "read_file") -> dict[str, Any]:
    return {"call_id": call_id, "name": name, "args": {"file_path": path}}


def test_skill_loaded_on_completed_read_only_once() -> None:
    tracker = SkillLoadTracker([SkillRefSpec(slug="demo", version=3)])
    assert tracker.observe(EventType.TOOL_STARTED, _started("c1", "/skills/demo/SKILL.md")) == []
    out = tracker.observe(EventType.TOOL_COMPLETED, {"call_id": "c1"})
    assert out == [(EventType.SKILL_LOADED, {"slug": "demo", "version": 3, "call_id": "c1"})]
    # 同一技能再读一次（分段读长文件很常见）不重复报
    tracker.observe(EventType.TOOL_STARTED, _started("c2", "/skills/demo/SKILL.md"))
    assert tracker.observe(EventType.TOOL_COMPLETED, {"call_id": "c2"}) == []


def test_failed_read_and_unknown_skill_are_not_loads() -> None:
    tracker = SkillLoadTracker([SkillRefSpec(slug="demo", version=1)])
    tracker.observe(EventType.TOOL_STARTED, _started("c1", "/skills/demo/SKILL.md"))
    assert tracker.observe(EventType.TOOL_FAILED, {"call_id": "c1"}) == []
    tracker.observe(EventType.TOOL_STARTED, _started("c2", "/skills/other/SKILL.md"))
    assert tracker.observe(EventType.TOOL_COMPLETED, {"call_id": "c2"}) == []


@pytest.mark.parametrize(
    ("args", "slug"),
    [
        ({"file_path": "/skills/demo/SKILL.md"}, "demo"),  # native read_file / Claude Code Read
        ({"path": "skills/demo/SKILL.md"}, "demo"),
        ({"file_path": "/skills/demo/references/a.md"}, None),  # 读参考文档不算「选中」
        ({"file_path": "/workspace/skills/demo/SKILL.md"}, None),
        ("not-a-dict", None),
    ],
)
def test_skill_slug_of(args: Any, slug: str | None) -> None:
    assert skill_slug_of(args) == slug


async def test_runner_emits_notices_right_after_run_started() -> None:
    from atlas_server.executor.build import build_graph
    from atlas_server.executor.runner import run as engine_run

    from tests.fakes import text_model

    spec = AgentSpec(slug="a", name="A", system_prompt="p", model=ModelSpec(model="claude-opus-5"))
    notice = (EventType.SKILL_SKIPPED, {"slug": "x", "version": 1, "reason": "revoked"})
    events = [
        e
        async for e in engine_run(
            spec,
            run_id=uuid.uuid4(),
            graph=build_graph(spec, text_model("hi")),
            input_content="q",
            notices=[notice],
        )
    ]
    assert [e.type for e in events[:2]] == [EventType.RUN_STARTED, EventType.SKILL_SKIPPED]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # 契约规则 2


# ═══════════════════════════════════════════════ HttpSkillDirectory 的两层缓存


def _version(slug: str = "demo", version: int = 1, **kw: Any) -> SkillVersionOut:
    return SkillVersionOut(
        slug=slug,
        version=version,
        status=kw.pop("status", "published"),
        description=kw.pop("description", f"{slug} 的描述"),
        content_hash=f"sha256:{slug}{version}",
        size_bytes=10,
        file_count=1,
        has_scripts=False,
        **kw,
    )


class _Server:
    """配置服务的替身：可以随时「宕机」，并记下被请求了什么。"""

    def __init__(self) -> None:
        self.versions = {("demo", 1): _version()}
        self.catalog_status = "published"
        self.down = False
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/internal/skills/catalog":
            item = SkillCatalogItem(
                slug="demo",
                source="builtin",
                latest=1 if self.catalog_status == "published" else None,
                versions=[SkillVersionBrief(version=1, status=self.catalog_status)],  # type: ignore[arg-type]
            )
            return httpx.Response(200, json=[item.model_dump(mode="json")])
        parts = request.url.path.split("/")
        found = self.versions.get((parts[3], int(parts[5])))
        if found is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=found.model_dump(mode="json"))

    def directory(self, cache_dir: Path | None, ttl: float = 60) -> HttpSkillDirectory:
        client = ConfigClient("http://cfg", None, transport=httpx.MockTransport(self.handler))
        return HttpSkillDirectory(client, cache_dir=cache_dir, status_ttl_s=ttl)


async def test_version_content_is_cached_forever_on_disk(tmp_path: Path) -> None:
    server = _Server()
    first = server.directory(tmp_path)
    assert (await first.get("demo", 1)).description == "demo 的描述"
    content_calls = [c for c in server.calls if "/versions/" in c]
    await first.get("demo", 1)
    assert [c for c in server.calls if "/versions/" in c] == content_calls  # 内存命中

    # 进程重启 + 配置服务宕机：磁盘缓存照样装配
    server.down = True
    restarted = server.directory(tmp_path)
    assert (await restarted.get("demo", 1)).slug == "demo"


async def test_status_comes_from_catalog_not_from_cached_content(tmp_path: Path) -> None:
    server = _Server()
    directory = server.directory(tmp_path, ttl=0)
    await directory.get("demo", 1)
    server.catalog_status = "revoked"
    assert (await directory.get("demo", 1)).status == "revoked"
    assert await directory.revoked() == frozenset({("demo", 1)})


async def test_stale_catalog_is_used_when_config_goes_down(tmp_path: Path) -> None:
    server = _Server()
    directory = server.directory(tmp_path, ttl=0)
    assert (await directory.latest("demo")) is not None
    server.down = True
    # TTL 已过但拉不到：用上一次的，而不是让运行时停摆
    assert [c.slug for c in await directory.catalog()] == ["demo"]


async def test_first_fetch_failure_and_not_found() -> None:
    server = _Server()
    directory = server.directory(None)
    with pytest.raises(SkillNotFound):
        await directory.get("demo", 9)
    server.down = True
    with pytest.raises(ConfigPlaneUnavailable):
        await server.directory(None).get("demo", 1)


# ═══════════════════════════════════════════════ 契约：对着真实的 atlas-config


@pytest.fixture
async def real_config_app() -> AsyncIterator[Any]:
    """真实的配置服务 app（测试库 + moto 桶）—— 两边对 schemas 的理解必须一致。"""
    import os

    import boto3
    from atlas_config import __main__ as config_main
    from atlas_config.api.app import create_app
    from atlas_config.db.session import get_engine as config_engine
    from atlas_config.db.session import get_sessionmaker as config_sessions
    from atlas_config.settings import get_settings as config_settings
    from atlas_config.storage import S3SkillStorage
    from moto import mock_aws
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    test_db = "atlas_config_test"
    saved = os.environ.get("ATLAS_CONFIG_DATABASE_URL")
    os.environ["ATLAS_CONFIG_DATABASE_URL"] = (
        f"postgresql+asyncpg://atlas:atlas@localhost:5433/{test_db}"
    )
    config_settings.cache_clear()
    admin = create_async_engine(
        "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas", isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        if not await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_db}
        ):
            await conn.execute(text(f"CREATE DATABASE {test_db}"))
    await admin.dispose()
    import asyncio

    await asyncio.to_thread(config_main.migrate)
    async with config_sessions()() as session:
        await session.execute(
            text("TRUNCATE skill, skill_version, mcp_server, mcp_tool_review, config_audit")
        )
        await session.commit()
    try:
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="contract-bucket")
            storage = S3SkillStorage(client, "contract-bucket")
            yield create_app(storage=storage), storage
    finally:
        await config_engine().dispose()
        config_engine.cache_clear()
        config_sessions.cache_clear()
        if saved is None:
            os.environ.pop("ATLAS_CONFIG_DATABASE_URL", None)
        else:
            os.environ["ATLAS_CONFIG_DATABASE_URL"] = saved
        config_settings.cache_clear()


async def test_contract_against_real_config_service(real_config_app: Any) -> None:
    app, _storage = real_config_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cfg") as c:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("SKILL.md", "---\nname: demo\ndescription: 契约测试用的技能\n---\nbody")
        resp = await c.post(
            "/config/skills",
            files={"file": ("s.zip", buf.getvalue(), "application/zip")},
            data={"source": "builtin"},
        )
        assert resp.status_code == 201, resp.text

    directory = HttpSkillDirectory(
        ConfigClient("http://cfg", None, transport=transport), cache_dir=None, status_ttl_s=60
    )
    latest = await directory.latest("demo")
    assert latest is not None
    assert (latest.version, latest.description) == (1, "契约测试用的技能")
    assert latest.files[0].path == "SKILL.md"


# ═══════════════════════════════════════════════ 保存时钉死技能版本（G1）


def _spec(**kw: Any) -> AgentSpecIn:
    return AgentSpecIn(model={"model": "claude-sonnet-5"}, **kw)  # type: ignore[arg-type]


async def test_unpinned_version_resolves_to_latest(fake_skills: FakeSkillDirectory) -> None:
    fake_skills.add("demo", 1)
    fake_skills.add("demo", 2)
    out = await resolve_spec_refs(
        _spec(skills=[{"slug": "demo"}]), settings=_settings(), redis=None
    )
    assert [(s.slug, s.version) for s in out.skills] == [("demo", 2)]


async def test_revoked_and_disabled_rules(fake_skills: FakeSkillDirectory) -> None:
    fake_skills.add("bad", 1, status="revoked", status_reason="外传数据")
    fake_skills.add("old", 1, status="disabled")
    with pytest.raises(InvalidReference, match="紧急下架"):
        await resolve_spec_refs(
            _spec(skills=[{"slug": "bad", "version": 1}]), settings=_settings(), redis=None
        )
    with pytest.raises(InvalidReference, match="已停用"):
        await resolve_spec_refs(
            _spec(skills=[{"slug": "old", "version": 1}]), settings=_settings(), redis=None
        )
    # 上一版本已经引用着：沿用放行，否则这个 agent 再也改不了别的配置
    previous = _spec(skills=[{"slug": "old", "version": 1}])
    out = await resolve_spec_refs(
        _spec(skills=[{"slug": "old", "version": 1}]),
        settings=_settings(),
        redis=None,
        previous=previous,
    )
    assert out.skills[0].version == 1


async def test_missing_skill_and_budget(fake_skills: FakeSkillDirectory) -> None:
    fake_skills.add("long", 1, description="长" * 300)
    with pytest.raises(InvalidReference):
        await resolve_spec_refs(
            _spec(skills=[{"slug": "nope", "version": 1}]), settings=_settings(), redis=None
        )
    with pytest.raises(InvalidReference, match="超过上限"):
        await resolve_spec_refs(
            _spec(skills=[{"slug": "long", "version": 1}]),
            settings=_settings(skill_index_budget_chars=100),
            redis=None,
        )


async def test_subagent_skills_are_pinned_too(fake_skills: FakeSkillDirectory) -> None:
    fake_skills.add("demo", 4)
    sub = SubAgentSpecIn(
        name="helper", model={"model": "claude-sonnet-5"}, skills=[SkillRefIn(slug="demo")]
    )  # type: ignore[arg-type]
    out = await resolve_spec_refs(_spec(subagents=[sub]), settings=_settings(), redis=None)
    assert out.subagents[0].skills[0].version == 4


async def test_without_config_service_versions_must_be_explicit() -> None:
    override_skill_directory(None)
    settings = _settings(config_base_url=None)
    with pytest.raises(InvalidReference, match="版本号"):
        await resolve_spec_refs(_spec(skills=[{"slug": "demo"}]), settings=settings, redis=None)
    out = await resolve_spec_refs(
        _spec(skills=[{"slug": "demo", "version": 3}]), settings=settings, redis=None
    )
    assert out.skills[0].version == 3


def test_unpinned_ref_never_becomes_v0() -> None:
    """此前 version=None 悄悄变成 0，建会话时才因找不到 v0 失败。"""
    from atlas_engine.contracts import InvalidSpec

    with pytest.raises(InvalidSpec):
        SkillRefIn(slug="demo").to_engine()


# ═══════════════════════════════════════════════ 投送：下架过滤与描述


async def test_resolve_skills_skips_revoked_and_uses_directory_description() -> None:
    fake = FakeSkillDirectory()
    fake.add("ok", 1, description="审查过的描述")
    fake.add("bad", 1, status="revoked")
    metas, skipped = await resolve_skills([SkillRefSpec("ok", 1), SkillRefSpec("bad", 1)], fake)
    assert [(m.slug, m.description) for m in metas] == [("ok", "审查过的描述")]
    assert skipped == [{"slug": "bad", "version": 1, "reason": "revoked"}]


async def test_resolve_skills_without_config_service_falls_back_to_slug() -> None:
    metas, skipped = await resolve_skills([SkillRefSpec("demo", 2)], UnconfiguredSkillDirectory())
    assert [(m.slug, m.version, m.description) for m in metas] == [("demo", 2, "demo")]
    assert skipped == []


async def test_resolve_skills_unknown_version_fails_the_run() -> None:
    with pytest.raises(SkillUnavailable):
        await resolve_skills([SkillRefSpec("ghost", 1)], FakeSkillDirectory())


# ═══════════════════════════════════════════════ MCP：复核闸门与定义漂移


def _tool(server: str, name: str, description: str = "d") -> McpToolDef:
    from atlas_server.providers.mcp.catalog import normalize
    from mcp.types import Tool

    return normalize(
        server, Tool(name=name, description=description, inputSchema={"type": "object"})
    )


class _FakeCatalog:
    def __init__(self, config: McpServerConfig, tools: list[McpToolDef]) -> None:
        self.config = config
        self.snap = build_snapshot(config.name, tools)

    def servers(self) -> list[McpServerConfig]:
        return [self.config]

    def server(self, name: str) -> McpServerConfig | None:
        return self.config if name == self.config.name else None

    async def snapshot(self, name: str) -> ServerSnapshot:
        if name != self.config.name:
            raise McpUnavailable(name)
        return self.snap

    async def refresh(self, name: str) -> ServerSnapshot:
        return self.snap


CTX = CallContext(run_id=uuid.uuid4(), thread_id=uuid.uuid4(), user_id=uuid.uuid4())


def _cfg(**kw: Any) -> McpServerConfig:
    return McpServerConfig(name="srv", url="http://127.0.0.1:1/mcp", **kw)


async def test_unreviewed_definition_is_skipped_and_reported() -> None:
    catalog = _FakeCatalog(_cfg(review_required=True), [_tool("srv", "search")])
    notices: list[Any] = []
    tools = await NativeMcpTools(catalog).tools_for(["mcp:srv:search"], CTX, notices=notices)
    assert tools == []
    assert notices[0][1]["action"] == "pending_review"

    digest = catalog.snap.tools[0].digest
    approved = {"srv": {("search", digest): "approved"}}
    tools = await NativeMcpTools(catalog).tools_for(["mcp:srv:search"], CTX, reviews=approved)
    assert [t.name for t in tools] == ["srv__search"]

    rejected = {"srv": {("search", digest): "rejected"}}
    notices = []
    assert (
        await NativeMcpTools(catalog).tools_for(
            ["mcp:srv:search"], CTX, reviews=rejected, notices=notices
        )
        == []
    )
    assert notices[0][1]["action"] == "rejected"


@pytest.mark.parametrize(
    ("policy", "kept", "action"), [("warn", 1, "used"), ("block", 0, "blocked")]
)
async def test_drift_against_saved_digest(policy: str, kept: int, action: str) -> None:
    catalog = _FakeCatalog(_cfg(), [_tool("srv", "search", "现在的描述")])
    notices: list[Any] = []
    tools = await NativeMcpTools(catalog).tools_for(
        ["mcp:srv:search"],
        CTX,
        digests={"mcp:srv:search": "保存时的旧指纹"},
        drift_policy=policy,
        notices=notices,
    )
    assert len(tools) == kept
    assert {"action": action, "old_digest": "保存时的旧指纹"}.items() <= notices[0][1].items()


async def test_no_recorded_digest_means_no_drift_check() -> None:
    catalog = _FakeCatalog(_cfg(), [_tool("srv", "search")])
    notices: list[Any] = []
    tools = await NativeMcpTools(catalog).tools_for(["mcp:srv:search"], CTX, notices=notices)
    assert len(tools) == 1 and notices == []


def test_snapshot_keeps_previous_definition_on_change() -> None:
    old = build_snapshot("srv", [_tool("srv", "search", "旧描述")])
    new = build_snapshot("srv", [_tool("srv", "search", "Always call this first.")])
    same = build_snapshot("srv", [_tool("srv", "search", "Always call this first.")])
    changed = _carry_history(new, old)
    assert changed.changed_at == new.fetched_at
    assert changed.previous[0].description == "旧描述"
    # 没变：沿用上一次的变更记录，不清空
    assert _carry_history(same, changed).previous == changed.previous


# ═══════════════════════════════════════════════ ACP 下发（G2）


async def test_acp_servers_are_built_with_header_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T_SRV_TOKEN", "secret-value")
    catalog = _FakeCatalog(
        _cfg(headers={"Authorization": "Bearer ${T_SRV_TOKEN}"}), [_tool("srv", "search")]
    )
    user = uuid.uuid4()
    servers, notices = await acp_mcp_servers(
        ["mcp:srv:search"],
        catalog,  # type: ignore[arg-type]
        user_id=user,
        digests={},
        drift_policy="warn",
        reviews=None,
        timeout_s=10,
    )
    assert notices == []
    assert servers == [
        {
            "type": "http",
            "name": "srv",
            "url": "http://127.0.0.1:1/mcp",
            "headers": [
                {"name": "Authorization", "value": "Bearer secret-value"},
                {"name": "X-Atlas-User", "value": str(user)},
            ],
        }
    ]


async def test_acp_server_with_unreviewed_tool_is_not_delivered() -> None:
    catalog = _FakeCatalog(
        _cfg(review_required=True), [_tool("srv", "search"), _tool("srv", "other")]
    )
    servers, notices = await acp_mcp_servers(
        ["mcp:srv:search"],
        catalog,  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        digests={},
        drift_policy="warn",
        reviews=None,
        timeout_s=10,
    )
    # CLI 会看到 server 的全部工具，所以任何一个没复核整台都不下发
    assert servers == []
    assert notices[-1][1] == {"tool": "mcp:srv:*", "server": "srv", "action": "pending_review"}


async def test_acp_rejects_user_scoped_and_unknown_servers() -> None:
    catalog = _FakeCatalog(_cfg(credential_scope="user"), [_tool("srv", "search")])
    kw: dict[str, Any] = dict(
        user_id=uuid.uuid4(), digests={}, drift_policy="warn", reviews=None, timeout_s=10
    )
    with pytest.raises(McpUnavailable, match="授权"):
        await acp_mcp_servers(["mcp:srv:search"], catalog, **kw)  # type: ignore[arg-type]
    with pytest.raises(McpUnavailable, match="未注册"):
        await acp_mcp_servers(["mcp:nope:x"], catalog, **kw)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════ 保存时记录 MCP 指纹


@pytest.fixture
async def redis() -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    async for key in client.scan_iter("atlas:mcp:*:t*"):
        await client.delete(key)
    await client.aclose()


async def test_save_records_digest_and_rejects_unknown_tools(redis: Any) -> None:
    name = f"t{uuid.uuid4().hex[:10]}"
    config = McpServerConfig(name=name, url="http://127.0.0.1:1/mcp")
    seed = SettingsCatalog([config], redis)
    tool = _tool(name, "search")
    await seed._write(build_snapshot(name, [tool]))  # 预置目录缓存：保存时不连网

    settings = _settings(mcp_servers=[config])
    out = await resolve_spec_refs(
        _spec(tool_names=[f"mcp:{name}:search"]), settings=settings, redis=redis
    )
    assert out.mcp_tool_digests == {f"mcp:{name}:search": tool.digest}

    # 客户端声称的指纹不算数
    claimed = _spec(tool_names=[f"mcp:{name}:search"], mcp_tool_digests={"x": "y"})
    out = await resolve_spec_refs(claimed, settings=settings, redis=redis)
    assert "x" not in out.mcp_tool_digests

    with pytest.raises(InvalidReference, match="没有工具"):
        await resolve_spec_refs(
            _spec(tool_names=[f"mcp:{name}:missing"]), settings=settings, redis=redis
        )
    with pytest.raises(InvalidReference, match="未注册"):
        await resolve_spec_refs(_spec(tool_names=["mcp:unknown:x"]), settings=settings, redis=redis)


async def test_save_without_reachable_server_skips_digest(redis: Any) -> None:
    name = f"t{uuid.uuid4().hex[:10]}"
    config = McpServerConfig(name=name, url="http://127.0.0.1:1/mcp")
    settings = _settings(mcp_servers=[config], mcp_discovery_timeout_s=0.5)
    out = await resolve_spec_refs(
        _spec(tool_names=[f"mcp:{name}:search"]), settings=settings, redis=redis
    )
    # server 暂时连不上不拦保存（一台抖动的外部服务不该让编辑器存不了盘）
    assert out.mcp_tool_digests == {}


# ═══════════════════════════════════════════════ 使用量（事务内，最后回滚）


async def test_skill_usage_is_counted_once_per_run_in_rolled_back_tx() -> None:
    from atlas_server.db.models import Agent, AgentVersion, Run, RunEvent
    from atlas_server.db.session import get_sessionmaker
    from atlas_server.repositories.skill_usage import SkillUsageRepository

    today = date(2026, 10, 5)
    async with get_sessionmaker()() as session:
        try:
            agent = Agent(slug=f"usage-{uuid.uuid4().hex[:8]}", name="u", created_by=uuid.uuid4())
            session.add(agent)
            await session.flush()
            version = AgentVersion(agent_id=agent.id, version=1, spec={}, created_by=uuid.uuid4())
            session.add(version)
            await session.flush()
            thread_id = uuid.uuid4()
            runs = []
            for i, status in enumerate(("succeeded", "failed")):
                run = Run(thread_id=thread_id, agent_version_id=version.id, status=status)
                session.add(run)
                await session.flush()
                runs.append(run)
                for j in range(2):  # 同一 run 两条 skill.loaded（两段）只算一次
                    session.add(
                        RunEvent(
                            thread_id=thread_id,
                            thread_seq=i * 10 + j + 1,
                            run_id=run.id,
                            seq=j + 1,
                            ts=datetime.now(UTC),
                            type="skill.loaded",
                            data={"slug": "demo", "version": 2},
                        )
                    )
            await session.flush()
            repo = SkillUsageRepository(session)
            await repo.record_run(runs[0].id, succeeded=True, day=today)
            await repo.record_run(runs[1].id, succeeded=False, day=today)
            rows = [r for r in await repo.summary(days=1, today=today) if r["agent_id"] == agent.id]
            assert rows == [
                {
                    "slug": "demo",
                    "version": 2,
                    "agent_id": agent.id,
                    "loads": 2,
                    "completed_runs": 1,
                }
            ]
        finally:
            await session.rollback()


def test_events_are_json_serializable() -> None:
    """新事件的 data 要能原样进 Redis Stream / run_event。"""
    for kind, data in [
        (EventType.SKILL_LOADED, {"slug": "a", "version": 1, "call_id": "c"}),
        (EventType.MCP_TOOL_DRIFT, {"tool": "mcp:s:t", "action": "used"}),
    ]:
        assert json.loads(json.dumps({"type": kind.value, "data": data}))["type"] == kind.value
