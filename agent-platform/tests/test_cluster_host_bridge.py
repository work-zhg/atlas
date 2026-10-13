"""cluster 模板：会话 Pod 的 bridge 容器（代码设计 §11）。纯模板，不碰数据库。"""

from __future__ import annotations

from typing import Any

from atlas_bridge.config import BridgeConfig
from atlas_cluster.config import ClusterSettings
from atlas_cluster.schemas import EnsurePodRequest
from atlas_cluster.template import TOKEN_FILE, pod_manifest


def _settings(**over: Any) -> ClusterSettings:
    return ClusterSettings(
        oss_bucket="atlas-oss",
        model_base_url="https://llm.example/anthropic",
        model_name="m-1",
        model_api_key="k",  # type: ignore[arg-type]
        **over,
    )


def _req() -> EnsurePodRequest:
    return EnsurePodRequest(
        thread_id="t-1",
        user_id="u-1",
        workspace_thread_id="t-1",
        image="atlas-acp-bridge:0.2.0",
        adapter="node /opt/acp-cli/claude-code-acp/dist/index.js",
        cli_type="claude-code",
    )


def _container() -> dict[str, Any]:
    return pod_manifest(_req(), _settings())["spec"]["containers"][0]


def test_bridge_container() -> None:
    c = _container()
    assert c["command"] == ["python", "-m", "atlas_bridge"]
    env = {e["name"]: e for e in c["env"]}
    assert env["ATLAS_BRIDGE_SESSION_ID"]["value"] == "t-1"
    assert env["ATLAS_BRIDGE_TOKEN_FILE"]["value"] == TOKEN_FILE
    # ★ token 不在环境变量里：环境变量会被 agent 继承（Bridge 设计 §8.2）
    assert "ATLAS_BRIDGE_TOKEN" not in env
    # 模型凭据仍从 Secret 注入 bridge 的环境，并且列进了传给 agent 的白名单
    assert "valueFrom" in env["ANTHROPIC_AUTH_TOKEN"]
    allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"]["value"].split(",")
    assert {"PATH", "HOME", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"} <= set(
        allow
    )
    assert len(allow) == len(set(allow))
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"


def test_bridge_container_keeps_the_hardened_security_context() -> None:
    sc = _container()["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}


def test_token_is_mounted_from_the_pod_secret() -> None:
    manifest = pod_manifest(_req(), _settings())
    volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
    assert volumes["bridge-token"]["secret"]["items"] == [{"key": "token", "path": "bridge-token"}]
    mounts = {m["name"]: m for m in manifest["spec"]["containers"][0]["volumeMounts"]}
    assert mounts["bridge-token"]["readOnly"] is True


def test_the_env_is_what_the_bridge_actually_reads() -> None:
    """★ 模板与 bridge 的配置解析对得上：用模板生成的环境变量构造 BridgeConfig。"""
    env = {e["name"]: e.get("value", "secret") for e in _container()["env"]}
    config = BridgeConfig.from_env(env)
    assert config.session_id == "t-1"
    assert config.agent_cmd == ("node", "/opt/acp-cli/claude-code-acp/dist/index.js")
    assert str(config.token_file) == TOKEN_FILE
    assert str(config.workspace) == "/workspace"
    assert "ANTHROPIC_AUTH_TOKEN" in config.agent_env_allow
