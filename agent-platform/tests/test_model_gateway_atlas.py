"""Atlas 经模型网关（doc/model-design.html）：Run 开始的检查、按目录的参数、平台用途、编辑器、保存。

真实的模型网关（atlas_modelgateway，自己的测试库 atlas_modelgateway_test）跑在后台线程里，
用户中心换成内存替身；它后面是假的 Higress AI 路由（同样在后台线程里，只认网关凭据）。
Atlas 用真实的 ChatAnthropic 经 HTTP 调网关 —— 整条「Atlas → 模型网关 → Higress」链路都是真的。

★ 网关的库只经它自己的 HTTP 接口写；断言用量时另开一个引擎读（网关的连接池属于它的线程）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from atlas_agent.config import get_settings
from atlas_agent.db.session import get_sessionmaker
from atlas_agent.executor.assembly import default_model_builder
from atlas_agent.executor.inprocess import InProcessExecutor
from atlas_agent.main import create_app
from atlas_agent.providers.llm.modelgateway import reset_model_gateway_cache
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.fake_uc import AS_USER, TEST_USER

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/modelgateway/tests"))
from fake_llm import FakeLLM, Profile, serve_in_thread
from modelgw_testkit import ADMIN, FakePermissions

GATEWAY_DB = "atlas_modelgateway_test"
_ADMIN_URL = "postgresql+asyncpg://atlas:atlas@localhost:5433/atlas"
_GATEWAY_DB_URL = f"postgresql+asyncpg://atlas:atlas@localhost:5433/{GATEWAY_DB}"
CREDENTIAL = "modelgw-atlas-test"
_TABLES = (
    "gw_model",
    "gw_model_price",
    "gw_model_probe",
    "gw_purpose",
    "gw_model_key",
    "gw_model_call",
    "gw_budget",
    "gw_audit",
)

_LLM = FakeLLM(credential=CREDENTIAL)
_PERMS = FakePermissions()


@pytest.fixture(scope="module")
def model_gateway() -> Iterator[str]:
    """起假的 Higress、真的模型网关（各一个后台线程），Atlas 的设置指向网关。→ 网关地址。"""
    import httpx as _httpx
    from atlas_modelgateway import auth as gateway_auth
    from atlas_modelgateway.settings import get_settings as gateway_settings

    def authz(request: _httpx.Request) -> _httpx.Response:
        uid = request.url.path.split("/")[-2]
        ops = ["model:manage"] if uid == ADMIN else []
        return _httpx.Response(
            200, json={"can_access": True, "permissions": ops, "user": {"status": "active"}}
        )

    saved_transport = gateway_auth._transport
    gateway_auth._transport = _httpx.MockTransport(authz)
    with serve_in_thread(_LLM.app()) as higress_url:
        overrides = {
            "ATLAS_MODELGW_DATABASE_URL": _GATEWAY_DB_URL,
            "ATLAS_MODELGW_HIGRESS_BASE_URL": higress_url,
            "ATLAS_MODELGW_GATEWAY_CREDENTIAL": CREDENTIAL,
            "ATLAS_MODELGW_MAINTAINER_TICK_S": "0",
            "ATLAS_MODELGW_UC_BASE_URL": "http://uc.invalid",
            # 收回授权立即生效（生产里有短缓存，§6.8）
            "ATLAS_MODELGW_PERM_CACHE_TTL_S": "0",
        }
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update(overrides)
        gateway_settings.cache_clear()

        async def _prepare() -> None:
            engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
            async with engine.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": GATEWAY_DB}
                )
                if not exists:
                    await conn.execute(text(f"CREATE DATABASE {GATEWAY_DB}"))
            await engine.dispose()

        asyncio.run(_prepare())
        from atlas_modelgateway.__main__ import migrate
        from atlas_modelgateway.api.app import create_app as gateway_app

        migrate()
        with serve_in_thread(gateway_app(perms=_PERMS)) as gateway_url:
            atlas_saved = os.environ.get("MODEL_GATEWAY_URL")
            os.environ["MODEL_GATEWAY_URL"] = gateway_url
            get_settings.cache_clear()
            try:
                yield gateway_url
            finally:
                if atlas_saved is None:
                    os.environ.pop("MODEL_GATEWAY_URL", None)
                else:
                    os.environ["MODEL_GATEWAY_URL"] = atlas_saved
                get_settings.cache_clear()
                reset_model_gateway_cache()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        gateway_settings.cache_clear()
    gateway_auth._transport = saved_transport


async def _gateway_db(sql: str, **params: Any) -> list[Any]:
    engine = create_async_engine(_GATEWAY_DB_URL)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest.fixture
async def gw(model_gateway: str) -> AsyncIterator[httpx.AsyncClient]:
    """模型网关的管理接口（以模型管理员的身份）。每个测试清空网关的库、替身和 Atlas 的缓存。"""
    await _gateway_db(f"TRUNCATE {', '.join(_TABLES)}")
    _LLM.models.clear()
    _LLM.seen.clear()
    _PERMS.grants.clear()
    _PERMS.registered.clear()
    _PERMS.users[TEST_USER] = {
        "id": TEST_USER,
        "name": "测试用户",
        "user_type": "human",
        "status": "active",
        "dept": None,
    }
    reset_model_gateway_cache()
    async with httpx.AsyncClient(base_url=model_gateway, headers={"X-Uid": ADMIN}) as c:
        yield c
    reset_model_gateway_cache()


async def _register(gw: httpx.AsyncClient, name: str, **caps: Any) -> None:
    _LLM.models[name] = Profile(supports=frozenset({"tools", "effort"}))
    r = await gw.post(
        "/api/v1/models",
        json={
            "name": name,
            "provider": "fake",
            "protocols": ["anthropic"],
            "context_window": 64_000,
            "max_output_tokens": 4_096,
            "capabilities": {"tools": True, "stream_usage": True, **caps},
        },
    )
    assert r.status_code == 201, r.text
    assert (await gw.post(f"/api/v1/models/{name}/activate")).status_code == 200


@pytest.fixture
async def atlas(gw: httpx.AsyncClient, clean_db: None) -> AsyncIterator[httpx.AsyncClient]:
    """Atlas：真实的模型构造（ChatAnthropic 经 HTTP 调模型网关）。"""
    app = create_app()
    app.state.executor = InProcessExecutor(
        get_sessionmaker(), get_settings(), model_builder=default_model_builder
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AS_USER
    ) as c:
        yield c


async def _agent(atlas: httpx.AsyncClient, model: str, **spec: Any) -> httpx.Response:
    return await atlas.post(
        "/v1/agents",
        json={
            "slug": f"via-{model}",
            "name": "经模型网关",
            "spec": {"system_prompt": "p", "model": {"model": model}, **spec},
        },
    )


async def _run(atlas: httpx.AsyncClient, agent_id: str) -> tuple[dict[str, Any], list[str]]:
    thread = (await atlas.post("/v1/threads", json={"agent_id": agent_id})).json()["id"]
    r = await atlas.post(
        f"/v1/threads/{thread}/runs", json={"content": [{"type": "text", "text": "你好"}]}
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    for _ in range(200):
        body = (await atlas.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    from uuid import UUID

    from atlas_agent.db.models import RunEvent
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        kinds = list(
            await session.scalars(
                select(RunEvent.type).where(RunEvent.run_id == UUID(run_id)).order_by(RunEvent.seq)
            )
        )
    return body, kinds


async def test_native_run_goes_through_the_gateway(gw, atlas) -> None:
    await _register(gw, "chat-a")
    await gw.put("/api/v1/purposes/title", json={"default_model": "chat-a"})
    _PERMS.allow("chat-a", TEST_USER)

    r = await _agent(atlas, "chat-a")
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    run, _ = await _run(atlas, agent_id)
    assert run["status"] == "succeeded", run

    # 主对话：带主体 Key；使用者、Agent、Run 只作参考；标题：平台用途，不带 Key
    rows = await _gateway_db(
        "SELECT purpose, subject_uid::text, agent_id, run_id, outcome, input_tokens "
        "FROM gw_model_call ORDER BY id"
    )
    chat = [r for r in rows if r[0] is None]
    title = [r for r in rows if r[0] == "title"]
    assert chat and chat[0][1] == TEST_USER and chat[0][2] == agent_id
    assert chat[0][3] == run["id"] and chat[0][4] == "ok" and chat[0][5] == 100
    assert title and title[0][1] is None and title[0][4] == "ok"
    # 到 Higress 的请求：网关凭据，没有主体 Key
    assert all(h.get("authorization") == f"Bearer {CREDENTIAL}" for _, h, _ in _LLM.seen[-2:])


async def test_run_does_not_start_without_a_grant(gw, atlas) -> None:
    await _register(gw, "chat-a")
    _PERMS.allow("chat-a", TEST_USER)
    agent_id = (await _agent(atlas, "chat-a")).json()["id"]
    _PERMS.grants["chat-a"].pop(TEST_USER)  # 保存之后被收回
    reset_model_gateway_cache()
    run, kinds = await _run(atlas, agent_id)
    assert run["status"] == "failed"
    assert "没有被授权使用模型 chat-a" in run["error_message"]
    assert kinds[:2] == ["run.started", "run.failed"]


async def test_deprecated_model_runs_with_a_notice(gw, atlas) -> None:
    await _register(gw, "chat-a")
    await _register(gw, "chat-b")
    _PERMS.allow("chat-a", TEST_USER)
    _PERMS.allow("chat-b", TEST_USER)
    agent_id = (await _agent(atlas, "chat-a")).json()["id"]
    await gw.post("/api/v1/models/chat-a/deprecate", json={"replacement": "chat-b"})
    reset_model_gateway_cache()
    run, kinds = await _run(atlas, agent_id)
    assert run["status"] == "succeeded"
    assert "model.deprecated" in kinds
    # 弃用的不能新选
    r = await atlas.post(
        "/v1/agents",
        json={
            "slug": "new-one",
            "name": "新的",
            "spec": {"system_prompt": "p", "model": {"model": "chat-a"}},
        },
    )
    assert r.status_code == 400 and "已弃用" in r.text


async def test_save_checks_grant_and_catalog_capabilities(gw, atlas) -> None:
    await _register(gw, "chat-a", effort=True)
    await _register(gw, "chat-b")
    _PERMS.allow("chat-a", TEST_USER)
    r = await _agent(atlas, "chat-b")
    assert r.status_code == 400 and "没有被授权使用模型：chat-b" in r.text
    r = await _agent(atlas, "nope")
    assert r.status_code == 400 and "不在模型网关的目录里" in r.text
    # 能力按目录判：chat-a 支持档位（写死的规则里它不是 Claude，原来会被拒）
    r = await atlas.post(
        "/v1/agents",
        json={
            "slug": "with-effort",
            "name": "档位",
            "spec": {"system_prompt": "p", "model": {"model": "chat-a", "effort": "low"}},
        },
    )
    assert r.status_code == 201, r.text
    r = await atlas.post(
        "/v1/agents",
        json={
            "slug": "with-temp",
            "name": "温度",
            "spec": {"system_prompt": "p", "model": {"model": "chat-a", "temperature": 0.3}},
        },
    )
    assert r.status_code == 400 and "temperature" in r.text


async def test_editor_lists_only_models_the_subject_can_use(gw, atlas) -> None:
    await _register(gw, "chat-a")
    await _register(gw, "chat-b")
    _PERMS.allow("chat-a", TEST_USER)
    r = await atlas.get("/v1/models")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["model"] for m in body["data"]] == ["chat-a"]
    assert body["subject"]["hidden_models"] == 1 and body["subject"]["kind"] == "user"
    # 以 Agent 为权限、还没有数字员工：列出全部，并提示保存后要授权
    body = (await atlas.get("/v1/models", params={"auth_subject": "agent"})).json()
    assert [m["model"] for m in body["data"]] == ["chat-a", "chat-b"]
    assert body["subject"]["notes"]


def test_acp_pod_gets_gateway_key_not_provider_key() -> None:
    from atlas_agent.domain.spec import CliSpec
    from atlas_agent.providers.opensandbox.acp import MODEL_KEY_ENV, acp_spec
    from atlas_agent.providers.pods import AcpModel

    spec = acp_spec(
        get_settings(),
        thread="t1",
        workspace_thread="t1",
        user=TEST_USER,
        token="tok",
        cli=CliSpec(cli_type="claude-code", adapter="a", image="img"),
        profile=None,
        model=AcpModel(
            base_url="http://gw:8090",
            key="gk-secret",
            model="chat-a",
            small_model="chat-a",
            headers={"X-Agent-Id": "a1"},
        ),
    )
    env = spec["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://gw:8090" and env["ANTHROPIC_MODEL"] == "chat-a"
    assert env[MODEL_KEY_ENV] == "gk-secret" and env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Agent-Id: a1"
    allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
    assert "ANTHROPIC_AUTH_TOKEN" in allow and MODEL_KEY_ENV not in allow
    script = spec["entrypoint"][-1]
    assert f'ANTHROPIC_AUTH_TOKEN="${MODEL_KEY_ENV}"' in script and f"-u {MODEL_KEY_ENV}" in script
    assert "/run/secrets/atlas/model_api_key" not in script  # 不再读挂进来的供应商 Key
