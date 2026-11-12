"""会话 Pod 领域（c4-model/agent/sandbox/code-sandbox-session-pod.html）。

一个人的 CLI 会话共用 Pod。

Pod 规格    没有会话特有的东西：入口是 execd、整个 {user}/ 挂到 /data、两个令牌分开、凭据注入
bridge      拉起它的环境变量 bridge 认得（会话、端口 = 8900 + 槽位、cwd、模型接入、git）；
            脚本真跑一遍：令牌写成文件、不留在 bridge 的环境里，模型 key 从挂载读出，记下 pid
确保        同一个人的会话共用 Pod、端口各不相同；满了 / 凭据集合不同另建；等 bridge 就绪、
            交出 ws 地址和服务令牌；bridge 退出了下一轮重新拉起；端口上是别的会话的旧 bridge
            先停掉；Pod 坏了重建；bridge 起不来只报这个会话的错，不动 Pod；凭据每轮整份重写
回收        停掉这个会话的 bridge 再解绑，最后一个会话走了才删 Pod；配额和原生沙箱合计；
            会话删除、孤儿回收、闲置回收（看 run 的状态）只释放自己的业务
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
from atlas_agent.sandbox.session_pod import BridgeLauncher, CliModel, PodRequest, SessionPods
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
        "workspace_dir": f"2026-11-12/{tid}",
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


def _bridge(req: PodRequest, slot: int = 0, **over: Any) -> tuple[str, dict[str, str]]:
    """拉起这个会话的 bridge 的脚本和环境变量。"""
    launcher = BridgeLauncher(_settings(**over), None, None)  # type: ignore[arg-type]
    return launcher._command(slot, req)


def _req(**over: Any) -> PodRequest:
    fields: dict[str, Any] = {
        "thread_uuid": uuid7(),
        "workspace_dir": "Q3 分析",
        "user_uuid": USER,
        "cli": CLI,
    }
    return PodRequest(**(fields | over))


# ──────────────────────────────────────────────── Pod 规格


def test_pod_spec_has_nothing_session_specific() -> None:
    """Pod 会被这个人的别的 CLI 会话共用：入口是 execd，挂整个 {user}/，会话的东西都不在里面。"""
    req = _req()
    body = _body(req)
    env = body["env"]
    assert body["entrypoint"] == ["tail", "-f", "/dev/null"] and env["EXECD_INIT"] == "1"
    (volume,) = body["volumes"]
    assert volume["mountPath"] == "/data" and volume["subPath"] == str(USER)
    assert volume["readOnly"] is False
    assert env["HOME"] == "/tmp/home"
    assert not any(k.startswith(("ATLAS_BRIDGE_SESSION", "ANTHROPIC_")) for k in env)
    assert str(req.thread_uuid) not in str(body)
    # ★ 两个令牌分开：服务令牌给 bridge，execd 令牌只在 Pod 里
    assert env["ATLAS_BRIDGE_TOKEN"] == "tok" and env["EXECD_ACCESS_TOKEN"] == "exec-tok"
    assert body["timeout"] is None
    # 每个沙箱都带出站策略；没有凭据就不开凭据注入
    assert body["networkPolicy"] == {"defaultAction": "allow"} and "credentialProxy" not in body


def test_credentials_enable_injection_for_the_pod() -> None:
    body = _body(_req(credentials=_granted()))
    assert body["networkPolicy"] == {"defaultAction": "allow"}
    assert body["credentialProxy"] == {"enabled": True}
    assert "gitee-secret" not in str(body)  # 凭据走 Vault


# ──────────────────────────────────────────────── bridge 的环境变量和脚本


def test_bridge_env_is_what_the_bridge_expects() -> None:
    """拉起命令的 env 必须能被 BridgeConfig 解析 —— 两边各写各的，漂移就是 bridge 起不来。"""
    req = _req()
    _, env = _bridge(req, slot=2)
    cfg = BridgeConfig.from_env(env)
    assert cfg.session_id == str(req.thread_uuid)
    assert cfg.listen_port == 8902  # 8900 + 槽位
    assert cfg.agent_cmd == ("node", "/opt/acp/index.js")
    assert str(cfg.token_file) == f"{pod_mod.BRIDGE_DIR}/{req.thread_uuid}.token"
    # ★ CLI 的 cwd 是会话的工作目录：建会话时定下、之后不变，Pod 重建前后一样
    assert str(cfg.workspace) == "/data/files/Q3 分析"
    agent = cfg.agent_env(env | {"ANTHROPIC_AUTH_TOKEN": "k", "PATH": "/bin"})
    assert agent["ANTHROPIC_BASE_URL"] == "https://llm.example/anthropic"
    assert agent["ANTHROPIC_MODEL"] == agent["ANTHROPIC_SMALL_FAST_MODEL"] == "m-1"
    assert agent["ANTHROPIC_AUTH_TOKEN"] == "k"  # 脚本从文件读出来的那个要放行
    assert agent["HOME"] == "/tmp/home"
    assert agent["CLAUDE_CONFIG_DIR"] == "/data/home/.claude"  # 一人一份，重建后接着跑
    assert "ATLAS_BRIDGE_TOKEN" not in agent and "EXECD_ACCESS_TOKEN" not in agent


def test_the_gateway_key_goes_to_the_bridge_and_vendor_keys_never_do() -> None:
    model = CliModel("https://gw", "sk-agent", "claude-x", "claude-x", {"X-Agent": "a"})
    req = _req(model=model)
    _, env = _bridge(req)
    assert env["ANTHROPIC_BASE_URL"] == "https://gw"
    assert env[pod_mod.MODEL_KEY_ENV] == "sk-agent"  # 只给这个会话的 bridge
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "sk-agent" not in str(_body(req))  # 不进 Pod 的规格：同一个 Pod 里可能是别的智能体


def test_cli_prompt_caching_is_off_unless_the_model_supports_it() -> None:
    """不支持缓存的模型收到 cache_control：LiteLLM 会翻成 Gemini 的上下文缓存，免费档整轮 429。"""
    _, env = _bridge(_req(model=CliModel("https://gw", "sk", "gemini-x", "gemini-x", {}, False)))
    assert env["DISABLE_PROMPT_CACHING"] == "1"
    assert "DISABLE_PROMPT_CACHING" in env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
    _, env = _bridge(_req(model=CliModel("https://gw", "sk", "claude-x", "claude-x", {})))
    assert "DISABLE_PROMPT_CACHING" not in env


def test_non_claude_cli_gets_no_model_env() -> None:
    script, env = _bridge(_req(cli=CliSpec(cli_type="codex", adapter="codex-acp", image="img")))
    assert not any(k.startswith("ANTHROPIC_") for k in env)
    assert "ANTHROPIC_AUTH_TOKEN" not in env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"]
    assert "model_api_key" not in script


def test_credentials_give_git_identity_and_trust_the_mitm_ca() -> None:
    _, env = _bridge(_req(credentials=_granted()))
    assert env["GIT_SSL_CAINFO"] == "/opt/opensandbox/merged-ca-certificates.pem"
    assert env["GIT_AUTHOR_NAME"] == "TeamFlow Agent"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@users.noreply.gitee.com"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        assert name in allow
    assert "gitee-secret" not in str(env)


def test_the_script_writes_the_token_file_and_hides_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真跑拉起脚本：PATH 里放一个假 python，它把自己看到的东西打印出来。"""
    monkeypatch.setattr(pod_mod, "BRIDGE_DIR", str(tmp_path / "bridge"))
    monkeypatch.setattr(pod_mod, "LOCAL_HOME", str(tmp_path / "home"))
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
    req = _req()
    script, env = _bridge(req)
    env |= {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ATLAS_BRIDGE_TOKEN": "tok-123",  # Pod 的环境变量（基座生成）
        "ATLAS_BRIDGE_WORKSPACE": str(tmp_path / "ws"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }
    subprocess.run(["/bin/sh", "-c", script], env=env, cwd=tmp_path, check=True)
    session = req.thread_uuid
    out = (tmp_path / "bridge" / f"{session}.log").read_text().splitlines()
    assert out == ["args=-m atlas_bridge", "token_env=unset", "model=sk-model", "tok-123"]
    token_file = tmp_path / "bridge" / f"{session}.token"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert (tmp_path / "bridge" / f"{session}.pid").read_text().strip().isdigit()
    assert (tmp_path / "ws").is_dir() and (tmp_path / "claude").is_dir()  # 新会话的目录先建好


# ──────────────────────────────────────────────── 确保


async def test_ensure_launches_the_bridge_and_hands_out_the_service_token(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    fake.bridge_unready = 3
    thread = _thread()
    ep = await _pods(fake, redis).ensure(thread, CLI)
    (sb,) = fake.created
    assert ep.url == f"ws://{sb.id}.execd:8900"
    assert sb.bridges == {8900: str(thread.thread_uuid)}
    assert ep.token == sb.spec["env"]["ATLAS_BRIDGE_TOKEN"]
    # 交出去的只有服务令牌；execd 令牌是另一个，只在 Pod 的环境里
    assert sb.spec["env"]["EXECD_ACCESS_TOKEN"] not in (ep.token, "")
    assert sb.bridge_unready == 0  # 等到 /readyz 回 200 才交出端点
    assert sb.spec["image"] == {"uri": CLI.image}


async def test_one_persons_sessions_share_a_pod_on_different_ports(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    pods = _pods(fake, redis)
    a, b = _thread(), _thread()
    ep_a, ep_b = await pods.ensure(a, CLI), await pods.ensure(b, CLI)
    (sb,) = fake.created
    assert (ep_a.url, ep_b.url) == (f"ws://{sb.id}.execd:8900", f"ws://{sb.id}.execd:8901")
    assert ep_a.token == ep_b.token  # 一个 Pod 一个令牌，会话由握手头 X-Session-Id 区分
    assert sb.bridges == {8900: str(a.thread_uuid), 8901: str(b.thread_uuid)}
    await pods.ensure(_thread(created_by_uuid=uuid7()), CLI)  # 别人另建
    assert len(fake.created) == 2


async def test_a_full_pod_or_other_credentials_get_another_pod(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    pods = _pods(fake, redis, cli_sessions_per_pod=2)
    for _ in range(3):
        await pods.ensure(_thread(), CLI)
    assert len(fake.created) == 2
    await pods.ensure(_thread(), CLI, credentials=_granted())  # 凭据注入整个 Pod 一份
    assert len(fake.created) == 3 and "credentialProxy" in fake.created[2].spec


async def test_the_next_turn_reuses_the_bridge(fake: FakeOpenSandbox, redis: Any) -> None:
    thread = _thread()
    first = await _pods(fake, redis).ensure(thread, CLI)
    second = await _pods(fake, redis).ensure(thread, CLI)  # 另一个进程
    assert first == second
    (sb,) = fake.created
    assert len(sb.commands) == 1  # 只拉起过一次


async def test_an_ended_bridge_is_launched_again_in_the_same_pod(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    """会话结束 bridge 就退出（不再带走 Pod）：下一轮端口上没人，重新拉起。

    CLI 按记下的会话 id 接着跑。
    """
    pods, thread = _pods(fake, redis), _thread()
    first = await pods.ensure(thread, CLI)
    (sb,) = fake.created
    sb.bridges.clear()
    assert await pods.ensure(thread, CLI) == first
    assert len(fake.created) == 1 and len(sb.commands) == 2


async def test_a_stale_bridge_of_another_session_is_stopped_first(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    """端口上是别的会话的 bridge（它释放时没停掉）：先停掉，再起自己的。"""
    pods, thread = _pods(fake, redis), _thread()
    await pods.ensure(thread, CLI)
    (sb,) = fake.created
    sb.bridges[8900] = "someone-else"
    sb.bridge_cmds["someone-else"] = next(iter(sb.commands))
    await pods.ensure(thread, CLI)
    assert sb.bridges == {8900: str(thread.thread_uuid)}


async def test_a_crashed_pod_is_replaced(fake: FakeOpenSandbox, redis: Any) -> None:
    """Pod 坏了（被驱逐、OOM）→ 下一轮退役、重建，bridge 在新 Pod 里重新拉起。"""
    pods, thread = _pods(fake, redis), _thread()
    first = await pods.ensure(thread, CLI)
    fake.created[0].state = "Failed"
    second = await pods.ensure(thread, CLI)
    assert second.url != first.url
    assert fake.deleted == [fake.created[0].id]


async def test_a_bridge_that_never_gets_ready_fails_only_this_session(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    """bridge 起不来只是这个会话的事：报错，Pod 留着（别的会话还在用）。"""
    pods = _pods(fake, redis, cli_ready_timeout_s=1.0)
    await pods.ensure(_thread(), CLI)
    fake.created[0].bridge_unready = 10_000
    with pytest.raises(TimeoutError, match="没有就绪"):
        await pods.ensure(_thread(), CLI)
    assert fake.deleted == []


async def test_a_bridge_that_exits_is_reported(
    fake: FakeOpenSandbox, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BridgeLauncher, "CHECK_EVERY_S", 0.0)
    fake.bridge_fails = True
    with pytest.raises(RuntimeError, match="启动失败"):
        await _pods(fake, redis).ensure(_thread(), CLI)


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


async def test_release_stops_the_bridge_and_the_last_one_deletes_the_pod(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    pods = _pods(fake, redis)
    a, b = _thread(), _thread()
    await pods.ensure(a, CLI)
    await pods.ensure(b, CLI)
    (sb,) = fake.created
    await pods.release(a.thread_uuid, USER)
    assert sb.bridges == {8901: str(b.thread_uuid)} and fake.deleted == []
    await pods.release(b.thread_uuid, USER)
    assert fake.deleted == [sb.id]


async def test_orphans_only_free_their_own_business(fake: FakeOpenSandbox, redis: Any) -> None:
    pods = _pods(fake, redis)
    live, orphan = _thread(), _thread()
    await pods.ensure(live, CLI)
    await pods.ensure(orphan, CLI)
    assert await pods.reap_orphans([live.thread_uuid]) == 1
    (sb,) = fake.created
    assert sb.bridges == {8900: str(live.thread_uuid)} and fake.deleted == []


async def test_idle_sessions_follow_the_runs_not_execd(fake: FakeOpenSandbox, redis: Any) -> None:
    c = component(fake, redis, **SETTINGS, cli_pod_idle_ttl_s=60.0)
    state: dict[UUID, tuple[bool, datetime | None]] = {}

    async def runs(thread_uuid: UUID) -> tuple[bool, datetime | None]:
        return state.get(thread_uuid, (False, None))

    settings = _settings(cli_pod_idle_ttl_s=60.0)
    launcher = BridgeLauncher(settings, c.runtime, c.exec, transport=fake.async_transport)
    pods = SessionPods(c.provisioning, launcher, settings, runs=runs)
    waiting, recent, idle = _thread(), _thread(), _thread()
    for t in (waiting, recent, idle):
        await pods.ensure(t, CLI)
    now = datetime.now(UTC)
    state[waiting.thread_uuid] = (True, now - timedelta(hours=5))  # 挂起等审批：不算闲
    state[recent.thread_uuid] = (False, now - timedelta(seconds=10))
    state[idle.thread_uuid] = (False, now - timedelta(hours=1))
    assert await pods.reap_idle() == 1
    (sb,) = fake.created
    assert str(idle.thread_uuid) not in sb.bridges.values() and len(sb.bridges) == 2
    assert fake.deleted == []  # 还有别的会话：Pod 留着


async def test_quota_counts_native_sandboxes_and_cli_pods_together(
    fake: FakeOpenSandbox, redis: Any
) -> None:
    c = component(fake, redis, **SETTINGS, sandbox_quota_per_user=2)
    await c.native.ensure(WorkspaceRef(uuid7(), USER), SandboxSpec())
    await c.pods.ensure(_thread(), CLI)
    await c.pods.ensure(_thread(), CLI)  # 共用同一个 Pod：不占新配额
    with pytest.raises(QuotaExceeded):
        await c.pods.ensure(_thread(), CLI, credentials=_granted())


async def test_a_launch_failure_is_reported(fake: FakeOpenSandbox, redis: Any) -> None:
    fake.create_state = "Failed"
    with pytest.raises(SandboxUnavailableError):
        await _pods(fake, redis).ensure(_thread(), CLI)

