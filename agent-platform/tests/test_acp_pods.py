"""ACP Pod 的供给：OpenSandboxPods / AcpProvisioner 对着假 OpenSandbox。

doc/sandbox-opensandbox-design.html §11、§12。

spec 契约      创建请求里的 bridge 环境能被 BridgeConfig 解析；三个前缀；模型 key 不进 spec
entrypoint     真跑那段 sh：令牌写成文件、模型 key 读成 env、令牌不留在 bridge 的环境里
ensure         等 bridge 就绪、复用、Failed 重建、ws:// 端点
git 档案       授权（allowed_users / allowed_apps）、networkPolicy + credentialProxy、
               Credential Vault
回收           release、孤儿、配额与原生沙箱合计
"""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from atlas_agent.config import GitProfile, get_settings
from atlas_agent.domain.spec import CliSpec
from atlas_agent.providers.opensandbox import (
    AcpProvisioner,
    GitProfileDenied,
    LifecycleError,
    OpenSandboxPods,
    QuotaExceeded,
    SandboxProvisioner,
    acp_spec,
)
from atlas_agent.providers.opensandbox import acp as acp_mod
from atlas_agent.redisx import make_redis
from atlas_bridge.config import BridgeConfig
from pydantic import SecretStr

from tests.opensandbox_fake import FakeOpenSandbox, fake_settings, wipe

USER = "u-1"
CLI = CliSpec(
    cli_type="claude-code", adapter="node /opt/acp/index.js", image="atlas-acp-bridge:0.4.0"
)
PROFILE = GitProfile(
    username="bot",
    token=SecretStr("gitee-secret"),
    author_name="TeamFlow Agent",
    allowed_apps=["teamflow"],
)


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[FakeOpenSandbox]:
    f = FakeOpenSandbox(tmp_path)
    yield f
    f.close()


@pytest.fixture
def tid() -> str:
    return f"t-{uuid4().hex[:10]}"


@pytest.fixture
async def redis(fake: FakeOpenSandbox, tid: str) -> AsyncIterator[Any]:
    client = make_redis(get_settings())
    yield client
    await wipe(client, tid, fake.prefix)
    await client.aclose()


def _settings(**over: Any) -> Any:
    base = {
        "acp_model_base_url": "https://llm.example/anthropic",
        "acp_model_name": "m-1",
        "git_profiles": {"teamflow-gitee": PROFILE},
        "acp_ready_timeout_s": 5.0,
    }
    return fake_settings(**(base | over))


def _thread(tid: str, **over: Any) -> Any:
    fields = {
        "id": tid,
        "created_by": USER,
        "workspace_thread_id": tid,
        "git_profile": "",
        "source_app": "",
    }
    return SimpleNamespace(**(fields | over))


def _pods(fake: FakeOpenSandbox, redis: Any, **over: Any) -> OpenSandboxPods:
    return OpenSandboxPods(_settings(**over), redis, transport=fake.async_transport)


# ──────────────────────────────────────────────── 创建请求的契约


def test_spec_is_what_the_bridge_expects() -> None:
    """创建请求里的 env 必须能被 BridgeConfig 解析 —— 两边各写各的，漂移就是 Pod 起不来。"""
    spec = acp_spec(
        _settings(),
        thread="t-1",
        workspace_thread="t-parent",
        user=USER,
        token="tok",
        cli=CLI,
        profile=None,
    )
    env = spec["env"]
    cfg = BridgeConfig.from_env(env)
    assert cfg.session_id == "t-1"
    assert cfg.listen_port == 8900
    assert cfg.agent_cmd == ("node", "/opt/acp/index.js")
    assert str(cfg.token_file) == acp_mod.TOKEN_FILE
    assert str(cfg.workspace) == "/workspace"
    agent = cfg.agent_env(env | {"ANTHROPIC_AUTH_TOKEN": "k", "PATH": "/bin"})
    assert agent["ANTHROPIC_BASE_URL"] == "https://llm.example/anthropic"
    assert agent["ANTHROPIC_MODEL"] == agent["ANTHROPIC_SMALL_FAST_MODEL"] == "m-1"
    assert agent["ANTHROPIC_AUTH_TOKEN"] == "k"  # entrypoint 从文件读出来的那个要放行
    assert agent["HOME"] == "/state"
    assert "ATLAS_BRIDGE_TOKEN" not in agent and "EXECD_ACCESS_TOKEN" not in agent
    # 令牌兼作 execd 令牌：execd 不能无鉴权
    assert env["EXECD_ACCESS_TOKEN"] == env["ATLAS_BRIDGE_TOKEN"] == "tok"
    assert spec["timeout"] is None
    assert "networkPolicy" not in spec and "credentialProxy" not in spec


def test_spec_prefixes_match_the_old_layout() -> None:
    """工作区挂父会话的、技能与状态挂自己的 —— 与 cluster 时代一致，存量会话能 resume。"""
    spec = acp_spec(
        _settings(),
        thread="t-sub",
        workspace_thread="t-parent",
        user=USER,
        token="tok",
        cli=CLI,
        profile=None,
    )
    mounts = {v["mountPath"]: v for v in spec["volumes"]}
    assert mounts["/workspace"]["subPath"] == f"{USER}/t-parent/workspace"
    assert mounts["/skills"]["subPath"] == f"{USER}/t-sub/skills" and mounts["/skills"]["readOnly"]
    assert mounts["/state"]["subPath"] == f"{USER}/t-sub/system/cli-home"
    assert spec["metadata"] == {
        "atlas.managed": "true",
        "atlas.kind": "acp",
        "atlas.thread": "t-sub",
        "atlas.workspace": "t-parent",
        "atlas.user": USER,
    }


def test_model_key_never_appears_in_the_spec() -> None:
    spec = acp_spec(
        _settings(),
        thread="t",
        workspace_thread="t",
        user=USER,
        token="tok",
        cli=CLI,
        profile=PROFILE,
    )
    assert "ANTHROPIC_AUTH_TOKEN" not in spec["env"]
    assert "gitee-secret" not in str(spec)  # git 凭据走 Credential Vault，也不进 spec


def test_non_claude_cli_gets_no_model_env() -> None:
    codex = CliSpec(cli_type="codex", adapter="codex-acp", image="img")
    spec = acp_spec(
        _settings(),
        thread="t",
        workspace_thread="t",
        user=USER,
        token="tok",
        cli=codex,
        profile=None,
    )
    assert not any(k.startswith("ANTHROPIC_") for k in spec["env"])
    assert "ANTHROPIC_AUTH_TOKEN" not in spec["env"]["ATLAS_BRIDGE_AGENT_ENV_ALLOW"]
    assert "model_api_key" not in " ".join(spec["entrypoint"])


def test_git_profile_spec_enables_vault_and_trusts_the_mitm_ca() -> None:
    spec = acp_spec(
        _settings(),
        thread="t",
        workspace_thread="t",
        user=USER,
        token="tok",
        cli=CLI,
        profile=PROFILE,
    )
    assert spec["networkPolicy"] == {"defaultAction": "allow"}
    assert spec["credentialProxy"] == {"enabled": True}
    env = spec["env"]
    assert env["GIT_SSL_CAINFO"] == acp_mod.MERGED_CA  # P0-3：不给 git 就过不了 MITM
    assert env["GIT_AUTHOR_NAME"] == "TeamFlow Agent"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@users.noreply.gitee.com"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
    for name in (
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "GIT_CONFIG_COUNT",
    ):
        assert name in allow


def test_entrypoint_writes_the_token_file_and_hides_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真跑 entrypoint：PATH 里放一个假 python，它把自己看到的东西打印出来。"""
    monkeypatch.setattr(acp_mod, "TOKEN_FILE", str(tmp_path / "bridge-token"))
    monkeypatch.setattr(acp_mod, "MODEL_KEY_FILE", str(tmp_path / "model_api_key"))
    (tmp_path / "model_api_key").write_text("sk-model\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text(
        '#!/bin/sh\necho "args=$*"\necho "token_env=${ATLAS_BRIDGE_TOKEN-unset}"\n'
        'echo "model=$ANTHROPIC_AUTH_TOKEN"\ncat "$ATLAS_BRIDGE_TOKEN_FILE"\n'
    )
    fake_python.chmod(0o755)
    spec = acp_spec(
        _settings(),
        thread="t",
        workspace_thread="t",
        user=USER,
        token="tok-123",
        cli=CLI,
        profile=None,
    )
    env = spec["env"] | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ATLAS_BRIDGE_TOKEN_FILE": acp_mod.TOKEN_FILE,
    }
    out = subprocess.run(
        spec["entrypoint"], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert out.splitlines() == [
        "args=-m atlas_bridge",
        "token_env=unset",
        "model=sk-model",
        "tok-123",
    ]
    assert stat.S_IMODE(os.stat(acp_mod.TOKEN_FILE).st_mode) == 0o600


# ──────────────────────────────────────────────── ensure


async def test_ensure_waits_for_the_bridge_and_returns_a_ws_endpoint(
    fake: FakeOpenSandbox, redis: Any, tid: str
) -> None:
    fake.bridge_unready = 3
    ep = await _pods(fake, redis).ensure(_thread(tid), CLI)
    (sb,) = fake.created
    assert ep.url == f"ws://{sb.id}.execd:8900"
    assert ep.token == sb.spec["env"]["ATLAS_BRIDGE_TOKEN"]
    assert sb.bridge_unready == 0  # 等到 /readyz 回 200 才交出端点
    assert sb.spec["image"] == {"uri": CLI.image}


async def test_second_run_reuses_the_pod(fake: FakeOpenSandbox, redis: Any, tid: str) -> None:
    pods = _pods(fake, redis)
    first = await pods.ensure(_thread(tid), CLI)
    second = await _pods(fake, redis).ensure(_thread(tid), CLI)  # 另一个进程
    assert first == second
    assert len(fake.created) == 1


async def test_crashed_pod_is_replaced(fake: FakeOpenSandbox, redis: Any, tid: str) -> None:
    """bridge / CLI 崩了 → 容器退出 → Failed（restartPolicy: Never）→ 下一轮删掉重建。

    CLI 从 /state resume。
    """
    pods = _pods(fake, redis)
    first = await pods.ensure(_thread(tid), CLI)
    fake.created[0].state = "Failed"
    second = await pods.ensure(_thread(tid), CLI)
    assert second.url != first.url
    assert fake.deleted == [fake.created[0].id]


async def test_bridge_that_never_gets_ready_fails_the_run(
    fake: FakeOpenSandbox, redis: Any, tid: str
) -> None:
    fake.bridge_unready = 10_000
    with pytest.raises(LifecycleError, match="没有就绪"):
        await _pods(fake, redis, acp_ready_timeout_s=1.0).ensure(_thread(tid), CLI)


async def test_missing_image_is_reported(fake: FakeOpenSandbox, redis: Any, tid: str) -> None:
    with pytest.raises(LifecycleError, match=r"cli\.image"):
        await _pods(fake, redis).ensure(_thread(tid), CliSpec(cli_type="claude-code"))
    assert fake.created == []


# ──────────────────────────────────────────────── git 档案


async def test_git_profile_is_bound_into_the_vault(
    fake: FakeOpenSandbox, redis: Any, tid: str
) -> None:
    await _pods(fake, redis).ensure(
        _thread(tid, git_profile="teamflow-gitee", source_app="teamflow"), CLI
    )
    (sb,) = fake.created
    (body,) = sb.vault_posts
    (cred,) = body["credentials"]
    assert base64.b64decode(cred["source"]["value"]).decode() == "bot:gitee-secret"
    (binding,) = body["bindings"]
    assert binding["match"]["hosts"] == ["gitee.com"]
    assert binding["auth"] == {"type": "basic", "credential": cred["name"]}


async def test_vault_is_rebound_when_missing_on_reuse(
    fake: FakeOpenSandbox, redis: Any, tid: str
) -> None:
    """建沙箱的进程崩在「建好」与「写 vault」之间：复用时补上；已经有了就不重复写。"""
    thread = _thread(tid, git_profile="teamflow-gitee", source_app="teamflow")
    pods = _pods(fake, redis)
    await pods.ensure(thread, CLI)
    await pods.ensure(thread, CLI)
    sb = fake.created[0]
    assert len(sb.vault_posts) == 1
    sb.vault = None
    await pods.ensure(thread, CLI)
    assert len(sb.vault_posts) == 2


@pytest.mark.parametrize(
    ("profile", "source_app", "match"),
    [
        ("no-such", "teamflow", "不存在"),
        ("teamflow-gitee", "", "只有 teamflow"),
        ("teamflow-gitee", "other-app", "只有 teamflow"),
    ],
)
async def test_git_profile_is_denied_before_anything_is_created(
    fake: FakeOpenSandbox, redis: Any, tid: str, profile: str, source_app: str, match: str
) -> None:
    with pytest.raises(GitProfileDenied, match=match):
        await _pods(fake, redis).ensure(
            _thread(tid, git_profile=profile, source_app=source_app), CLI
        )
    assert fake.created == []


async def test_git_profile_allowed_users(fake: FakeOpenSandbox, redis: Any, tid: str) -> None:
    profiles = {
        "p": GitProfile(username="bot", token=SecretStr("x"), allowed_users=["someone-else"])
    }
    with pytest.raises(GitProfileDenied, match="无权"):
        await _pods(fake, redis, git_profiles=profiles).ensure(_thread(tid, git_profile="p"), CLI)


# ──────────────────────────────────────────────── 回收与配额


async def test_release_and_orphans(fake: FakeOpenSandbox, redis: Any, tid: str) -> None:
    settings = _settings(sandbox_reap_min_age_s=0.0)
    p = AcpProvisioner(settings, redis, transport=fake.async_transport)
    await p.ensure(_thread(tid), CLI)
    await p.ensure(_thread(f"{tid}-orphan"), CLI)
    await p.reap_orphans({tid})
    assert [sb.id for sb in fake.running()] == [fake.created[0].id]
    await p.release(tid)
    assert fake.running() == []
    assert await p.routes.get(tid, fresh=True) is None


async def test_quota_counts_native_sandboxes_and_acp_pods_together(
    fake: FakeOpenSandbox, redis: Any, tid: str
) -> None:
    settings = _settings(sandbox_quota_per_user=2)
    await SandboxProvisioner(settings, redis, transport=fake.async_transport).ensure(
        f"{tid}-ws", user=USER
    )
    await AcpProvisioner(settings, redis, transport=fake.async_transport).ensure(_thread(tid), CLI)
    with pytest.raises(QuotaExceeded, match="已有 2 个"):
        await AcpProvisioner(settings, redis, transport=fake.async_transport).ensure(
            _thread(f"{tid}-b"), CLI
        )
