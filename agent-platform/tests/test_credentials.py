"""凭据组件和基座的网络与凭据（改造计划 B4）。

凭据组件（c4-model/agent/code-credentials.html）
    档案       通用化为注入方式（basic、bearer、api_key）+ 主机；规则必填
    授权       check 建会话时校验，resolve 每轮重新授权，available 不含凭据值
    会话       建会话时选定、校验（不存在或无权 403），子会话继承；旧字段 git_profile 仍收

网络与凭据（c4-model/agent/sandbox/code-sandbox-base-network.html）
    写凭据     整份覆盖：没有就 POST，有了就一次 PATCH 换成这一份；空列表等于清空；等 egress 监听
    改策略     PUT /policy，默认动作总是显式的
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from atlas_agent.config import CredentialProfileIn, get_settings
from atlas_agent.credentials import (
    CredentialDenied,
    CredentialGrants,
    CredentialProfileStore,
    CredentialSubject,
    GrantRule,
)
from atlas_agent.ids import uuid7
from atlas_agent.sandbox.base.lifecycle.server import Endpoint
from atlas_agent.sandbox.base.network.egress import EgressError, OpenSandboxEgress
from atlas_agent.sandbox.base.network.policy import NetworkPolicy
from atlas_agent.sandbox.base.network.vault import CredentialBinding, vault_body
from pydantic import SecretStr, ValidationError

from tests.opensandbox_fake import FakeOpenSandbox, FakeSandbox

PROFILES = {
    "repo-bot": {
        "auth": "basic",
        "hosts": ["gitee.com"],
        "username": "bot",
        "secret": "gitee-token",
        "apps": ["teamflow"],
        "author_name": "TeamFlow Agent",
        "author_email": "bot@example.com",
    },
    "npm": {
        "auth": "bearer",
        "hosts": ["npm.example.com"],
        "secret": "npm-token",
        "users": ["u-1"],
    },
    "api": {
        "auth": "api_key",
        "hosts": ["api.example.com"],
        "secret": "k",
        "header": "X-Api-Key",
        "users": ["u-1"],
        "apps": ["teamflow"],
    },
}


def _settings(**over: Any) -> Any:
    return get_settings().model_copy(
        update={
            "credential_profiles": {k: CredentialProfileIn(**v) for k, v in PROFILES.items()},
        }
        | over
    )


def _grants(**over: Any) -> CredentialGrants:
    return CredentialGrants(CredentialProfileStore.from_settings(_settings(**over)))


# ──────────────────────────────────────────────── 档案与授权


@pytest.mark.parametrize(
    ("profile", "match"),
    [
        ({"auth": "bearer", "hosts": ["h"], "secret": "s"}, "users 或 apps"),
        ({"auth": "basic", "hosts": ["h"], "secret": "s", "apps": ["a"]}, "username"),
        ({"auth": "api_key", "hosts": ["h"], "secret": "s", "apps": ["a"]}, "header"),
        ({"auth": "bearer", "hosts": [], "secret": "s", "apps": ["a"]}, "hosts"),
        ({"auth": "bearer", "hosts": ["h"], "secret": "", "apps": ["a"]}, "secret"),
    ],
)
def test_a_profile_that_does_not_validate_fails_to_load(profile: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CredentialProfileIn(**profile)


def test_grant_rules_need_at_least_one_restriction_and_require_both_when_both_are_set() -> None:
    with pytest.raises(ValueError):
        GrantRule()
    both = GrantRule(frozenset({"u-1"}), frozenset({"teamflow"}))
    assert both.allows("u-1", "teamflow")
    assert not both.allows("u-1", "")
    assert not both.allows("u-2", "teamflow")


def test_resolve_turns_authorized_profiles_into_vault_bindings() -> None:
    creds = _grants().resolve(CredentialSubject("u-1", "teamflow", ("repo-bot", "npm", "api")))
    assert creds.codes == {"repo-bot", "npm", "api"} and not creds.empty()
    body = vault_body(list(creds.bindings))
    by_name = {b["name"]: b for b in body["bindings"]}
    assert by_name["repo-bot"]["auth"] == {"type": "basic", "credential": "repo-bot"}
    assert by_name["npm"]["auth"] == {"type": "bearer", "credential": "npm"}
    assert by_name["api"]["auth"] == {"type": "apiKey", "credential": "api", "name": "X-Api-Key"}
    secrets = {c["name"]: c["source"]["value"] for c in body["credentials"]}
    assert base64.b64decode(secrets["repo-bot"]).decode() == "bot:gitee-token"
    assert secrets["npm"] == "npm-token"
    assert creds.git_author is not None and creds.git_author.name == "TeamFlow Agent"
    assert _grants().resolve(CredentialSubject("u-1", "", ())).empty()


@pytest.mark.parametrize(
    ("subject", "match"),
    [
        (CredentialSubject("u-1", "teamflow", ("nope",)), "不存在"),
        (CredentialSubject("u-1", "", ("repo-bot",)), "只有 teamflow"),
        (CredentialSubject("u-2", "", ("npm",)), "无权"),
        (CredentialSubject("u-1", "", ("api",)), "只有 teamflow"),  # 两项都写：都要满足
    ],
)
def test_wrong_or_unauthorized_profiles_are_denied_not_dropped(
    subject: CredentialSubject, match: str
) -> None:
    with pytest.raises(CredentialDenied, match=match):
        _grants().check(subject)
    with pytest.raises(CredentialDenied):
        _grants().resolve(subject)


def test_available_lists_codes_and_hosts_without_secrets() -> None:
    shown = _grants().available("u-1", "teamflow")
    assert [(p.code, p.auth, p.hosts) for p in shown] == [
        ("api", "api_key", ("api.example.com",)),
        ("npm", "bearer", ("npm.example.com",)),
        ("repo-bot", "basic", ("gitee.com",)),
    ]
    assert [p.code for p in _grants().available("u-2", "")] == []
    assert "token" not in repr(shown)


# ──────────────────────────────────────────────── 写凭据、改策略


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


class Locator:
    def __init__(self, fake: FakeOpenSandbox) -> None:
        self.fake = fake
        self.boxes: dict[UUID, FakeSandbox] = {}

    def add(self, *, egress: bool = True) -> tuple[UUID, FakeSandbox]:
        sid = uuid7()
        spec: dict[str, Any] = {"metadata": {"atlas.thread": str(sid)}}
        if egress:
            spec["credentialProxy"] = {"enabled": True}
        self.boxes[sid] = self.fake.add(spec)
        return sid, self.boxes[sid]

    async def locate(self, sandbox_uuid: UUID) -> Endpoint:
        sb = self.boxes[sandbox_uuid]
        return Endpoint(
            f"http://{sb.id}.execd:18080", {"OPENSANDBOX-EGRESS-AUTH": f"egress-{sb.id}"}
        )


@pytest.fixture
def locator(fake: FakeOpenSandbox) -> Locator:
    return Locator(fake)


def _egress(fake: FakeOpenSandbox, locator: Locator, **kw: Any) -> OpenSandboxEgress:
    return OpenSandboxEgress(locator, transport=fake.async_transport, poll_s=0.01, **kw)


def _b(name: str, token: str = "t") -> CredentialBinding:
    return CredentialBinding.bearer(name, [f"{name}.example.com"], SecretStr(token))


async def test_bind_overwrites_the_whole_vault_every_time(
    fake: FakeOpenSandbox, locator: Locator
) -> None:
    sid, sb = locator.add()
    egress = _egress(fake, locator)
    await egress.bind(sid, [_b("a"), _b("b")])
    assert [b["name"] for b in sb.vault["bindings"]] == ["a", "b"]
    assert len(sb.vault_posts) == 1

    # 第二轮：换了令牌、去掉 b、加了 c —— 一次 PATCH 原子地换成这一份
    await egress.bind(sid, [_b("a", "rotated"), _b("c")])
    assert len(sb.vault_posts) == 1 and len(sb.vault_patches) == 1
    assert sorted(b["name"] for b in sb.vault["bindings"]) == ["a", "c"]
    values = {c["name"]: c["source"]["value"] for c in sb.vault["credentials"]}
    assert values == {"a": "rotated", "c": "t"}

    await egress.bind(sid, [])  # 空列表 = 清空（权限被收回）
    assert sb.vault is None


async def test_bind_waits_until_egress_listens(fake: FakeOpenSandbox, locator: Locator) -> None:
    sid, sb = locator.add()
    fake.break_execd(3)  # egress 晚于主容器起来：先连不上几次
    await _egress(fake, locator).bind(sid, [_b("a")])
    assert sb.vault is not None

    sid2, _ = locator.add(egress=False)  # 没有 sidecar：等满时限报错
    with pytest.raises(EgressError, match="没有就绪"):
        await _egress(fake, locator, ready_timeout_s=0.05).bind(sid2, [_b("a")])


async def test_update_policy_puts_an_explicit_default(
    fake: FakeOpenSandbox, locator: Locator
) -> None:
    sid, sb = locator.add()
    await _egress(fake, locator).update_policy(sid, NetworkPolicy.deny_all().allowing("pypi.org"))
    assert sb.policy == {
        "defaultAction": "deny",
        "egress": [{"action": "allow", "target": "pypi.org"}],
    }


# ──────────────────────────────────────────────── 会话


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    from atlas_agent.config import get_settings as real
    from atlas_agent.main import create_app
    from httpx import ASGITransport

    settings = _settings()
    monkeypatch.setattr("atlas_agent.config.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[real] = lambda: settings
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _agent(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    body = {
        "slug": f"cred-{uuid7().hex[:8]}",
        "name": "凭据测试",
        "spec": {"system_prompt": "p", "model": {"model": "claude-opus-5"}},
    }
    r = await client.post("/v1/agents", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_threads_pick_profiles_at_creation_and_children_inherit(
    client: httpx.AsyncClient, clean_db: None
) -> None:
    from atlas_agent.db.models import Thread
    from atlas_agent.db.session import fetch_one, get_sessionmaker
    from atlas_agent.repositories.thread import ThreadRepository

    from tests.fake_uc import AS_USER

    as_teamflow = AS_USER | {"X-Caller-App": "teamflow"}
    aid = await _agent(client, AS_USER)

    # 人直接建：repo-bot 只给 TeamFlow 建的会话用
    r = await client.post(
        "/v1/threads", json={"agent_id": aid, "credential_profiles": ["repo-bot"]}, headers=AS_USER
    )
    assert r.status_code == 403 and r.json()["error"]["kind"] == "credential_denied", r.text
    r = await client.post(
        "/v1/threads", json={"agent_id": aid, "credential_profiles": ["nope"]}, headers=AS_USER
    )
    assert r.status_code == 403 and "不存在" in r.json()["error"]["message"]

    # TeamFlow 建：旧字段 git_profile 照收，和新字段合并
    r = await client.post(
        "/v1/threads", json={"agent_id": aid, "git_profile": "repo-bot"}, headers=as_teamflow
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["credential_profiles"] == ["repo-bot"] and body["git_profile"] == "repo-bot"

    async with get_sessionmaker()() as session:
        repo = ThreadRepository(session)
        parent = await fetch_one(session, Thread, Thread.thread_uuid == UUID(body["id"]))
        assert parent is not None
        child = await repo.create(
            agent_id=parent.agent_uuid,
            title="子",
            created_by=parent.created_by_uuid,
            parent_thread_id=parent.thread_uuid,
            subagent_name="helper",
        )
        await session.commit()
        assert child.credential_profiles == ["repo-bot"]  # 子会话继承


async def test_the_profile_list_shows_only_what_the_caller_may_pick(
    client: httpx.AsyncClient,
) -> None:
    from tests.fake_uc import AS_USER

    r = await client.get("/v1/credential-profiles", headers=AS_USER | {"X-Caller-App": "teamflow"})
    assert r.status_code == 200, r.text
    assert [p["code"] for p in r.json()] == ["repo-bot"]
    assert "secret" not in r.text and "gitee-token" not in r.text
