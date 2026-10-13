"""cluster：原生智能体沙箱 Pod（doc/native-sandbox-design.html §5 / §7.2 / §12）。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from atlas_cluster.backend import InMemoryBackend
from atlas_cluster.config import ClusterSettings, GitProfile
from atlas_cluster.manager import ImageDenied, PodManager
from atlas_cluster.schemas import EnsurePodRequest
from atlas_cluster.template import (
    LABEL_KIND,
    pod_name_for,
    sandbox_pod_manifest,
    secret_manifest,
    secret_name_for,
)


def _settings(**over: Any) -> ClusterSettings:
    return ClusterSettings(
        oss_bucket="atlas-oss",
        model_api_key="sk-model",  # 沙箱里不该出现
        git_profiles={"gitee": GitProfile(secret="git-gitee")},
        **over,
    )


def _req(thread_id: str = "ws-1", **over: Any) -> EnsurePodRequest:
    return EnsurePodRequest(
        thread_id=thread_id,
        user_id="u-1",
        workspace_thread_id=thread_id,
        kind="sandbox",
        **over,
    )


def _containers(m: dict[str, Any]) -> dict[str, dict[str, Any]]:
    spec = m["spec"]
    return {c["name"]: c for c in [*spec.get("initContainers", []), *spec["containers"]]}


def test_sandbox_pod_is_named_and_labelled_by_kind() -> None:
    m = sandbox_pod_manifest(_req(), _settings())
    assert m["metadata"]["name"] == pod_name_for("ws-1", "sandbox") == "atlas-sbx-ws-1"
    assert m["metadata"]["labels"][LABEL_KIND] == "sandbox"
    assert m["spec"]["automountServiceAccountToken"] is False
    assert m["spec"]["enableServiceLinks"] is False


def test_main_container_has_no_credentials() -> None:
    m = sandbox_pod_manifest(_req(), _settings())
    main = _containers(m)["sandbox"]
    # 没有任何来自 Secret 的环境变量（模型、对象存储、rc 口令都不在这里）
    assert not [e for e in main["env"] if "valueFrom" in e]
    names = {e["name"] for e in main["env"]}
    assert not names & {
        "ANTHROPIC_AUTH_TOKEN",
        "RCLONE_RC_PASS",
        "RCLONE_CONFIG_OSS_SECRET_ACCESS_KEY",
    }
    # 令牌只以只读文件出现
    token_mount = next(v for v in main["volumeMounts"] if v["name"] == "sandbox-token")
    assert token_mount["readOnly"]


def test_secret_has_rc_password_but_no_model_key() -> None:
    s = secret_manifest(_req(), "tok", _settings())
    assert s["metadata"]["name"] == secret_name_for("ws-1", "sandbox")
    assert s["stringData"]["token"] == "tok"
    assert s["stringData"]["rc_pass"]
    assert "model_api_key" not in s["stringData"]


def test_rc_password_only_reaches_mounter_and_barrier() -> None:
    holders = {
        name
        for name, c in _containers(sandbox_pod_manifest(_req(), _settings())).items()
        if any(e["name"] == "RCLONE_RC_PASS" for e in c.get("env", []))
    }
    assert holders == {"oss-workspace", "barrier"}


def test_workspace_mounter_has_barrier_flags() -> None:
    c = _containers(sandbox_pod_manifest(_req(), _settings()))
    ws = c["oss-workspace"]["args"][0]
    assert "--attr-timeout 0" in ws and "--rc-addr 127.0.0.1:5572" in ws and "--rc-user sbx" in ws
    assert "--rc-no-auth" not in ws
    # 技能挂载不需要远程控制
    assert "--rc" not in c["oss-skills"]["args"][0]
    assert c["barrier"]["restartPolicy"] == "Always"
    assert c["barrier"]["command"] == ["/dist/bin/sandboxd", "barrier"]


def test_main_container_is_locked_down_and_runs_injected_daemon() -> None:
    main = _containers(sandbox_pod_manifest(_req(), _settings()))["sandbox"]
    sc = main["securityContext"]
    assert sc["runAsNonRoot"] and sc["readOnlyRootFilesystem"]
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"] == {"drop": ["ALL"]}
    assert main["command"][0] == "/opt/atlas/bin/tini"
    assert main["image"] == "atlas-sandbox:dev"
    assert main["resources"]["limits"]["memory"] == "4Gi"
    mounts = {v["mountPath"]: v for v in main["volumeMounts"]}
    assert mounts["/skills"]["readOnly"] and mounts["/opt/atlas"]["readOnly"]
    assert mounts["/workspace"]["mountPropagation"] == "HostToContainer"
    env = {e["name"]: e["value"] for e in main["env"]}
    assert env["SANDBOXD_BARRIER_URL"] == "http://127.0.0.1:5573"
    assert env["SANDBOXD_REQUIRE_MOUNT"] == "1"


def test_installer_runs_before_sidecars() -> None:
    init = [c["name"] for c in sandbox_pod_manifest(_req(), _settings())["spec"]["initContainers"]]
    assert init[0] == "sandboxd-installer"


def test_git_credentials_are_mounted_only_on_demand() -> None:
    plain = _containers(sandbox_pod_manifest(_req(), _settings()))["sandbox"]
    assert "GIT_CONFIG_GLOBAL" not in {e["name"] for e in plain["env"]}
    withgit = _containers(sandbox_pod_manifest(_req(git_profile="gitee"), _settings()))["sandbox"]
    env = {e["name"]: e["value"] for e in withgit["env"]}
    assert env["GIT_CONFIG_GLOBAL"] == "/run/secrets/git/gitconfig"
    mount = next(v for v in withgit["volumeMounts"] if v["mountPath"] == "/run/secrets/git")
    assert mount["readOnly"]


def test_no_object_storage_means_no_barrier() -> None:
    m = sandbox_pod_manifest(_req(), _settings(workspace_mount="none"))
    names = set(_containers(m))
    assert names == {"sandboxd-installer", "sandbox"}
    env = {e["name"]: e["value"] for e in _containers(m)["sandbox"]["env"]}
    assert "SANDBOXD_BARRIER_URL" not in env and env["SANDBOXD_REQUIRE_MOUNT"] == "0"


def test_csi_is_refused_rather_than_pretending_consistency() -> None:
    with pytest.raises(ValueError, match="csi"):
        sandbox_pod_manifest(_req(), _settings(workspace_mount="csi"))


def test_no_secret_value_leaks_into_manifest() -> None:
    dumped = json.dumps(sandbox_pod_manifest(_req(), _settings(oss_secret_access_key="oss-secret")))
    assert "oss-secret" not in dumped and "sk-model" not in dumped


# ─────────────────────────────────────────────── manager


async def test_ensure_sandbox_returns_sandboxd_url_and_is_idempotent() -> None:
    backend = InMemoryBackend()
    mgr = PodManager(backend, _settings())
    first = await mgr.ensure(_req())
    again = await mgr.ensure(_req())
    assert first.created and not again.created
    assert first.url == "http://10.0.0.1:8910"
    assert first.token == again.token
    assert first.pod_name == "atlas-sbx-ws-1"


async def test_acp_and_sandbox_pods_of_one_thread_do_not_collide() -> None:
    backend = InMemoryBackend()
    mgr = PodManager(backend, _settings())
    sbx = await mgr.ensure(_req("t-1"))
    acp = await mgr.ensure(
        EnsurePodRequest(
            thread_id="t-1",
            user_id="u-1",
            workspace_thread_id="t-1",
            image="atlas-acp:1",
            adapter="x",
            cli_type="claude-code",
        )
    )
    assert sbx.pod_name != acp.pod_name and acp.created
    assert set(backend.pods) == {"atlas-sbx-t-1", "atlas-sess-t-1"}
    await mgr.release("t-1")
    assert backend.pods == {} and backend.secrets == {}


async def test_sandbox_image_must_be_allowed() -> None:
    mgr = PodManager(InMemoryBackend(), _settings(sandbox_image_allow_prefixes=["registry.local/"]))
    with pytest.raises(ImageDenied):
        await mgr.ensure(_req(image="evil/miner:latest"))
    ok = await mgr.ensure(_req("ws-2", image="registry.local/tbench/task:1"))
    assert ok.created
