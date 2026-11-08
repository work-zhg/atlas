"""会话 Pod 领域（改造计划 B6，c4-model/agent/sandbox/code-sandbox-session-pod.html）。

创建规格   bridge 认得的环境变量、两个令牌分开、三个挂载前缀、模型接入、凭据注入与 MITM CA 信任
entrypoint 真跑一遍：令牌写成文件、不留在 bridge 的环境里，模型 key 从挂载读出
确保       等 bridge 就绪、交出 ws 地址和服务令牌；复用；坏了重建；起不来报错；凭据每轮整份重写
回收       配额和原生沙箱合计；会话删除、孤儿回收、闲置回收（看 run 的状态）只释放自己的业务
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from atlas_agent.config import CredentialProfileIn, get_settings
from atlas_agent.credentials import (
    CredentialGrants,
    CredentialProfileStore,
    CredentialSet,
    CredentialSubject,
)
from atlas_agent.ids import uuid7
from atlas_agent.redisx import make_redis
from atlas_agent.sandbox import session_pod as pod_mod
from atlas_agent.sandbox.base.lifecycle.server import request_body
from atlas_agent.sandbox.native import WorkspaceRef
from atlas_agent.sandbox.provisioning.model import BizRef, BizType, QuotaExceeded
from atlas_agent.sandbox.provisioning.service import SandboxUnavailableError
from atlas_agent.sandbox.session_pod import CliModel, PodRequest, SessionPods
from atlas_bridge.config import BridgeConfig
from atlas_engine.contracts.spec import CliSpec, SandboxSpec
from pydantic import SecretStr

from tests.opensandbox_fake import FakeOpenSandbox
from tests.sandbox_env import component

USER = uuid7()
CLI = CliSpec(
    cli_type="claude-code", adapter="node /opt/acp/index.js", image="atlas-cli-bridge:0.4.0"
)
PROFILES = {
    "repo-bot": CredentialProfileIn(
        auth="basic",
        hosts=["gitee.com"],
        username="bot",
        secret=SecretStr("gitee-secret"),
        apps=["teamflow"],
        author_name="TeamFlow Agent",
        author_email="bot@users.noreply.gitee.com",
    )
}
SETTINGS = {
    "cli_model_base_url": "https://llm.example/anthropic",
    "cli_model_name": "m-1",
    "credential_profiles": PROFILES,
    "cli_ready_timeout_s": 5.0,
}


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
async def redis(clean_db: None) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await client.aclose()


def _thread(**over: Any) -> Any:
    tid = uuid7()
    fields = {
        "thread_uuid": tid,
        "workspace_thread_id": tid,
        "created_by_uuid": USER,
        "credential_profiles": [],
        "source_app": "",
    }
    return SimpleNamespace(**(fields | over))


def _granted() -> CredentialSet:
    grants = CredentialGrants(CredentialProfileStore.from_settings(_settings()))
    return grants.resolve(CredentialSubject(str(USER), "teamflow", ("repo-bot",)))


def _settings(**over: Any) -> Any:
    from tests.opensandbox_fake import fake_settings

    return fake_settings(**(SETTINGS | over))


def _pods(fake: FakeOpenSandbox, redis: Any, **over: Any) -> SessionPods:
    return component(fake, redis, **(SETTINGS | over)).pods


def _body(req: PodRequest, **over: Any) -> dict[str, Any]:
    """CliPodKind 构造的规格 → 发给 OpenSandbox 的请求体（令牌由基座生成，这里给两个假的）。"""
    from atlas_agent.sandbox.session_pod import CliPodKind

    kind = CliPodKind(_settings(**over), None, None)  # type: ignore[arg-type]
    spec = kind.build(req, BizRef(BizType.CLI_AGENT, req.thread_uuid, req.user_uuid))
    return request_body(spec, SecretStr("exec-tok"), SecretStr("tok"))


def _req(**over: Any) -> PodRequest:
    fields: dict[str, Any] = {
        "thread_uuid": uuid7(),
        "workspace_uuid": uuid7(),
        "user_uuid": USER,
        "cli": CLI,
    }
    return PodRequest(**(fields | over))


# ──────────────────────────────────────────────── 创建规格


def test_spec_is_what_the_bridge_expects() -> None:
    """创建请求里的 env 必须能被 BridgeConfig 解析 —— 两边各写各的，漂移就是 Pod 起不来。"""
    req = _req()
    body = _body(req)
    env = body["env"]
    cfg = BridgeConfig.from_env(env)
    assert cfg.session_id == str(req.thread_uuid)
    assert cfg.listen_port == 8900
    assert cfg.agent_cmd == ("node", "/opt/acp/index.js")
    assert str(cfg.token_file) == pod_mod.TOKEN_FILE
    assert str(cfg.workspace) == "/workspace"
    agent = cfg.agent_env(env | {"ANTHROPIC_AUTH_TOKEN": "k", "PATH": "/bin"})
    assert agent["ANTHROPIC_BASE_URL"] == "https://llm.example/anthropic"
    assert agent["ANTHROPIC_MODEL"] == agent["ANTHROPIC_SMALL_FAST_MODEL"] == "m-1"
    assert agent["ANTHROPIC_AUTH_TOKEN"] == "k"  # entrypoint 从文件读出来的那个要放行
    assert agent["HOME"] == "/state"
    assert "ATLAS_BRIDGE_TOKEN" not in agent and "EXECD_ACCESS_TOKEN" not in agent
    # ★ 两个令牌分开：服务令牌给 bridge，execd 令牌只在 Pod 里
    assert env["ATLAS_BRIDGE_TOKEN"] == "tok" and env["EXECD_ACCESS_TOKEN"] == "exec-tok"
    assert body["timeout"] is None
    # 每个沙箱都带出站策略；没有凭据就不开凭据注入
    assert body["networkPolicy"] == {"defaultAction": "allow"} and "credentialProxy" not in body


def test_mounts_follow_the_workspace_layout() -> None:
    """工作区挂根会话的、技能与 CLI 的 HOME 挂自己的。"""
    req = _req()
    mounts = {v["mountPath"]: v for v in _body(req)["volumes"]}
    me, root = req.thread_uuid, req.workspace_uuid
    assert mounts["/workspace"]["subPath"] == f"{USER}/{root}/workspace"
    assert mounts["/skills"]["subPath"] == f"{USER}/{me}/skills" and mounts["/skills"]["readOnly"]
    assert mounts["/state"]["subPath"] == f"{USER}/{me}/system/cli-home"


def test_the_gateway_key_goes_in_as_a_secret_and_vendor_keys_never_do() -> None:
    model = CliModel("https://gw", "sk-agent", "claude-x", "claude-x", {"X-Agent": "a"})
    env = _body(_req(model=model))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://gw"
    assert env[pod_mod.MODEL_KEY_ENV] == "sk-agent"  # 机密环境变量（基座并进 env）
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "gitee-secret" not in str(_body(_req(credentials=_granted())))  # 凭据走 Vault


def test_non_claude_cli_gets_no_model_env() -> None:
    body = _body(_req(cli=CliSpec(cli_type="codex", adapter="codex-acp", image="img")))
    assert not any(k.startswith("ANTHROPIC_") for k in body["env"])
    assert "ANTHROPIC_AUTH_TOKEN" not in body["env"]["ATLAS_BRIDGE_AGENT_ENV_ALLOW"]
    assert "model_api_key" not in " ".join(body["entrypoint"])


def test_credentials_enable_injection_and_trust_the_mitm_ca() -> None:
    body = _body(_req(credentials=_granted()))
    assert body["networkPolicy"] == {"defaultAction": "allow"}
    assert body["credentialProxy"] == {"enabled": True}
    env = body["env"]
    assert env["GIT_SSL_CAINFO"] == "/opt/opensandbox/merged-ca-certificates.pem"
    assert env["GIT_AUTHOR_NAME"] == "TeamFlow Agent"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@users.noreply.gitee.com"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        assert name in allow


def test_entrypoint_writes_the_token_file_and_hides_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真跑 entrypoint：PATH 里放一个假 python，它把自己看到的东西打印出来。"""
    monkeypatch.setattr(pod_mod, "TOKEN_FILE", str(tmp_path / "bridge-token"))
    monkeypatch.setattr(pod_mod, "MODEL_KEY_FILE", str(tmp_path / "model_api_key"))
    (tmp_path / "model_api_key").write_text("sk-model\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text(
        '#!/bin/sh\necho "args=$*"\necho "token_env=${ATLAS_BRIDGE_TOKEN-unset}"\n'
        'echo "model=$ANTHROPIC_AUTH_TOKEN"\ncat "$ATLAS_BRIDGE_TOKEN_FILE"\n'
    )
    fake_python.chmod(0o755)
    body = _body(_req())
    env = body["env"] | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ATLAS_BRIDGE_TOKEN": "tok-123",
        "ATLAS_BRIDGE_TOKEN_FILE": pod_mod.TOKEN_FILE,
    }
    out = subprocess.run(body["entrypoint"], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.splitlines() == [
        "args=-m atlas_bridge",
        "token_env=unset",
        "model=sk-model",
        "tok-123",
    ]
    assert stat.S_IMODE(os.stat(pod_mod.TOKEN_FILE).st_mode) == 0o600


# ──────────────────────────────────────────────── 确保


async def test_ensure_waits_for_the_bridge_and_hands_out_the_service_token(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    fake.bridge_unready = 3
    ep = await _pods(fake, redis).ensure(_thread(), CLI)
    (sb,) = fake.created
    assert ep.url == f"ws://{sb.id}.execd:8900"
    assert ep.token == sb.spec["env"]["ATLAS_BRIDGE_TOKEN"]
    # 交出去的只有服务令牌；execd 令牌是另一个，只在 Pod 的环境里
    assert sb.spec["env"]["EXECD_ACCESS_TOKEN"] not in (ep.token, "")
    assert sb.bridge_unready == 0  # 等到 /readyz 回 200 才交出端点
    assert sb.spec["image"] == {"uri": CLI.image}


async def test_the_next_turn_reuses_the_pod(fake: FakeOpenSandbox, redis: Any) -> None:
    thread = _thread()
    first = await _pods(fake, redis).ensure(thread, CLI)
    second = await _pods(fake, redis).ensure(thread, CLI)  # 另一个进程
    assert first == second
    assert len(fake.created) == 1


async def test_a_crashed_pod_is_replaced(fake: FakeOpenSandbox, redis: Any) -> None:
    """bridge / CLI 崩了 → 容器退出 → Failed（restartPolicy: Never）→ 下一轮退役、重建。

    CLI 从持久化的 HOME 接着跑。
    """
    pods, thread = _pods(fake, redis), _thread()
    first = await pods.ensure(thread, CLI)
    fake.created[0].state = "Failed"
    second = await pods.ensure(thread, CLI)
    assert second.url != first.url
    assert fake.deleted == [fake.created[0].id]


async def test_a_bridge_that_never_gets_ready_fails_and_frees_the_pod(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    fake.bridge_unready = 10_000
    with pytest.raises(TimeoutError, match="没有就绪"):
        await _pods(fake, redis, cli_ready_timeout_s=1.0).ensure(_thread(), CLI)
    assert fake.deleted == [fake.created[0].id]


async def test_missing_image_is_reported(fake: FakeOpenSandbox, redis: Any) -> None:
    with pytest.raises(ValueError, match=r"cli\.image"):
        await _pods(fake, redis).ensure(_thread(), CliSpec(cli_type="claude-code"))
    assert fake.created == []


async def test_credentials_are_rewritten_into_the_vault_every_turn(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    """每轮整份写一遍：崩在「建好」和「写凭据」之间能补上，换了令牌下一轮就生效。"""
    pods, thread = _pods(fake, redis), _thread()
    await pods.ensure(thread, CLI, credentials=_granted())
    (sb,) = fake.created
    (body,) = sb.vault_posts
    (cred,) = body["credentials"]
    assert base64.b64decode(cred["source"]["value"]).decode() == "bot:gitee-secret"
    (binding,) = body["bindings"]
    assert binding["match"]["hosts"] == ["gitee.com"]
    assert binding["auth"] == {"type": "basic", "credential": cred["name"]}

    await pods.ensure(thread, CLI, credentials=_granted())  # 下一轮：已经有了 → 一次 PATCH 换掉
    assert len(sb.vault_posts) == 1 and len(sb.vault_patches) == 1
    sb.vault = None  # 暂停恢复、或建好后没写进去
    await pods.ensure(thread, CLI, credentials=_granted())
    assert len(sb.vault_posts) == 2


# ──────────────────────────────────────────────── 回收与配额


async def test_release_and_orphans_only_free_their_own_business(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    pods = _pods(fake, redis)
    live, orphan = _thread(), _thread()
    await pods.ensure(live, CLI)
    await pods.ensure(orphan, CLI)
    assert await pods.reap_orphans([live.thread_uuid]) == 1
    assert [sb.id for sb in fake.running()] == [fake.created[0].id]
    await pods.release(live.thread_uuid, USER)
    assert fake.running() == []


async def test_idle_pods_follow_the_runs_not_execd(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis, **SETTINGS, cli_pod_idle_ttl_s=60.0)
    state: dict[UUID, tuple[bool, datetime | None]] = {}

    async def runs(thread_uuid: UUID) -> tuple[bool, datetime | None]:
        return state.get(thread_uuid, (False, None))

    pods = SessionPods(c.provisioning, c.runtime, _settings(cli_pod_idle_ttl_s=60.0), runs=runs)
    waiting, recent, idle = _thread(), _thread(), _thread()
    for t in (waiting, recent, idle):
        await pods.ensure(t, CLI)
    now = datetime.now(UTC)
    state[waiting.thread_uuid] = (True, now - timedelta(hours=5))  # 挂起等审批：不算闲
    state[recent.thread_uuid] = (False, now - timedelta(seconds=10))
    state[idle.thread_uuid] = (False, now - timedelta(hours=1))
    assert await pods.reap_idle() == 1
    assert len(fake.deleted) == 1 and len(fake.running()) == 2


async def test_quota_counts_native_sandboxes_and_cli_pods_together(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    c = component(fake, redis, **SETTINGS, sandbox_quota_per_user=2)
    await c.native.ensure(WorkspaceRef(uuid7(), USER), SandboxSpec())
    await c.pods.ensure(_thread(), CLI)
    with pytest.raises(QuotaExceeded):
        await c.pods.ensure(_thread(), CLI)


async def test_a_launch_failure_is_reported(fake: FakeOpenSandbox, redis: Any) -> None:
    fake.create_state = "Failed"
    with pytest.raises(SandboxUnavailableError):
        await _pods(fake, redis).ensure(_thread(), CLI)
