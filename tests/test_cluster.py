"""cluster 服务：模板、配额、幂等、回收（acp 详设 §12 步骤 5）。

★ 本机没有 K8s 集群，用 InMemoryBackend —— 被测的是这个服务的**主体**
  （模板渲染、幂等、配额、孤儿判定），K8s 调用只是末端。真集群验证留给
  部署环境（执行环境 §12 的待验证清单）。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_cluster.api import create_app
from atlas_cluster.backend import InMemoryBackend
from atlas_cluster.config import ClusterSettings
from atlas_cluster.manager import PodManager, QuotaExceeded
from atlas_cluster.schemas import EnsurePodRequest
from atlas_cluster.template import (
    LABEL_MANAGED,
    LABEL_THREAD,
    LABEL_USER,
    pod_manifest,
    pod_name_for,
    secret_manifest,
)
from httpx import ASGITransport


def _settings(**over) -> ClusterSettings:
    return ClusterSettings(oss_bucket="atlas-oss", **over)


def _req(thread_id: str = "t-1", *, user_id: str = "u-1", workspace: str | None = None):
    return EnsurePodRequest(
        thread_id=thread_id,
        user_id=user_id,
        workspace_thread_id=workspace or thread_id,
        image="atlas-acp:1",
        adapter="node claude-code-acp",
        cli_type="claude-code",
    )


# ──────────────────────────────────────────────── 模板


def test_workspace_and_skills_come_from_different_threads_under_csi() -> None:
    """★ 这是整个模板里最要紧的一条（subagent §03 / 文件系统 §05）。

    workspace 挂**父会话的**前缀（子智能体看得见父与兄弟的产物），
    skills 挂**自己的**且只读（技能跟着 agent 走）。两个前缀来自不同
    thread —— 这正是「读共享、技能隔离」是结构性而非约定的原因：
    挂载根本带不上别人的技能。

    csi 模式下这个区分落在 subPath 上。
    """
    manifest = pod_manifest(
        _req("t-child", workspace="t-parent"), _settings(workspace_mount="csi")
    )
    mounts = {m["mountPath"]: m for m in manifest["spec"]["containers"][0]["volumeMounts"]}

    assert mounts["/workspace"]["subPath"] == "t-parent/workspace"
    assert mounts["/workspace"].get("readOnly", False) is False

    assert mounts["/skills"]["subPath"] == "t-child/skills"
    assert mounts["/skills"]["readOnly"] is True


def test_workspace_and_skills_come_from_different_threads_under_fuse() -> None:
    """★ 同一条语义在 fuse 模式下的落点 —— 挂载器的 rclone remote。

    两种模式必须说同一件事。分头实现最容易出的错就是改了一边忘了另一边，
    而错的方向是静默的：子智能体读不到父的产物、或者读到了父的技能。
    """
    manifest = pod_manifest(_req("t-child", workspace="t-parent"), _settings())
    mounters = {c["name"]: c for c in manifest["spec"]["initContainers"]}

    workspace_cmd = mounters["oss-workspace"]["args"][0]
    assert "u-1/t-parent/workspace" in workspace_cmd
    assert "--read-only" not in workspace_cmd

    skills_cmd = mounters["oss-skills"]["args"][0]
    assert "u-1/t-child/skills" in skills_cmd
    assert "--read-only" in skills_cmd


def test_fuse_mounters_are_native_sidecars_gated_on_the_mount() -> None:
    """★ 挂载必须**先于** bridge 就绪，否则 adapter 的 cwd 是个空目录。

    两层保证，缺一不可：
      · initContainers + restartPolicy=Always（原生 sidecar）→ 先起、不退出
      · startupProbe 查 /proc/mounts → 进程起来了不等于挂上了

    只有第一层的话，rclone 拉起来但还在跟对象存储握手时 bridge 就开跑了，
    adapter 写的文件会被随后建立的 FUSE 挂载整个遮住 —— 不报错，只是从此
    谁也看不见。
    """
    manifest = pod_manifest(_req(), _settings())
    mounters = manifest["spec"]["initContainers"]
    assert [c["name"] for c in mounters] == ["oss-workspace", "oss-state", "oss-skills"]
    for mounter in mounters:
        assert mounter["restartPolicy"] == "Always"
        assert "/proc/mounts" in mounter["startupProbe"]["exec"]["command"][-1]


def test_bridge_gets_the_mount_propagated_into_it() -> None:
    """★ HostToContainer 不可省。

    没有它，bridge 容器拿到的是卷在启动瞬间的视图，而 FUSE 挂载是在那
    之后建立的 —— 目录会一直是空的，而且不报错。
    """
    manifest = pod_manifest(_req(), _settings())
    mounts = {m["mountPath"]: m for m in manifest["spec"]["containers"][0]["volumeMounts"]}

    assert mounts["/workspace"]["mountPropagation"] == "HostToContainer"
    assert mounts["/skills"]["mountPropagation"] == "HostToContainer"
    assert mounts["/skills"]["readOnly"] is True


def test_object_storage_credentials_never_appear_in_the_manifest() -> None:
    """★ 凭据走 secretKeyRef，不进 Pod spec 的明文。

    manifest 的明文谁有 pod read 权限谁就看得见，还会原样躺在 etcd 与
    各种 `kubectl get -o yaml` 的排查输出里。token 一直是这么做的，
    凭据没理由例外。
    """
    import json

    settings = _settings(
        oss_access_key_id="GOOG-FAKE-KEY-ID",
        oss_secret_access_key="fake-secret-value",
    )
    rendered = json.dumps(pod_manifest(_req(), settings))

    assert "GOOG-FAKE-KEY-ID" not in rendered
    assert "fake-secret-value" not in rendered
    # 但确实取到了 —— 否则挂载器根本连不上桶
    assert "RCLONE_CONFIG_OSS_ACCESS_KEY_ID" in rendered
    assert "secretKeyRef" in rendered


def test_credentials_are_withheld_from_the_bridge_container() -> None:
    """★ 凭据只给挂载器，不给 bridge。

    bridge 里跑的是 adapter，而 adapter 会执行模型生成的命令 —— 凭据只要
    出现在它的环境里，一条 `env` 就能读走整把 key。而这把 key 的权限是
    **整桶**的，不是本会话前缀的：读走了就等于拿到所有会话的数据。
    """
    manifest = pod_manifest(_req(), _settings())
    bridge_env = {e["name"] for e in manifest["spec"]["containers"][0]["env"]}

    assert not any(name.startswith("RCLONE_") for name in bridge_env)
    assert "OSS_BUCKET" not in bridge_env


def test_no_object_storage_means_an_ephemeral_workspace() -> None:
    """none 模式：不挂桶，工作区随 Pod 生灭。

    对应 server 侧 workspace_configured=False 的那种部署。挂载点仍然在 ——
    adapter 总要有个 cwd —— 只是背后没有对象存储。
    """
    manifest = pod_manifest(_req(), _settings(workspace_mount="none"))
    spec = manifest["spec"]

    assert "initContainers" not in spec
    assert {v["name"] for v in spec["volumes"]} == {"workspace", "skills", "state", "tmp"}
    assert all("emptyDir" in v for v in spec["volumes"])
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert {"/workspace", "/skills"} <= mounts


def test_the_adapter_gets_its_model_endpoint_and_credentials() -> None:
    """★ 真 CLI 要靠这组环境变量才能认证。

    早先模板只注入 ATLAS_*，于是**真 CLI 根本没法认证** —— 而假 adapter
    不调模型，所以这个缺口一路没被发现。接上真 Claude Code 的第一下就撞出来。
    """
    settings = _settings(
        model_base_url="https://api.deepseek.com/anthropic",
        model_name="deepseek-chat",
        model_api_key="sk-fake",
    )
    env = {e["name"]: e for e in pod_manifest(_req(), settings)["spec"]["containers"][0]["env"]}

    assert env["ANTHROPIC_BASE_URL"]["value"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_MODEL"]["value"] == "deepseek-chat"
    # ★ 小模型也要指过去：不指的话 CLI 会去找端点上不存在的 haiku，
    #   失败发生在生成标题之类的旁路上，很难联想到模型配置。
    assert env["ANTHROPIC_SMALL_FAST_MODEL"]["value"] == "deepseek-chat"
    # 凭据走 Secret，不进 manifest 明文
    assert "value" not in env["ANTHROPIC_AUTH_TOKEN"]
    assert env["ANTHROPIC_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "model_api_key"


def test_an_unknown_cli_type_gets_no_anthropic_variables() -> None:
    """变量名是 claude-code 的约定，别的 CLI 各有各的一套。

    塞一堆它不认的变量进去，只会让"为什么没生效"更难查 —— 宁可让它因为
    缺凭据明确失败。
    """
    settings = _settings(model_base_url="https://x/anthropic", model_api_key="sk-fake")
    request = EnsurePodRequest(
        thread_id="t-1",
        user_id="u-1",
        workspace_thread_id="t-1",
        image="i",
        adapter="a",
        cli_type="codex",
    )
    env = {e["name"] for e in pod_manifest(request, settings)["spec"]["containers"][0]["env"]}

    assert not any(name.startswith("ANTHROPIC_") for name in env)
    assert "HOME" in env  # 但 HOME 总要有 —— 根是只读的


def test_the_cli_state_directory_is_persistent() -> None:
    """★ resume 的全部依据。

    CLI 把会话记录写在 HOME 下。HOME 落在 emptyDir 上的话，resume 只在
    Pod 活着时成立 —— 而 Pod 会被闲置回收，于是表现成**悄悄失忆**：
    恢复"成功"了，但上一轮不见了。
    """
    manifest = pod_manifest(_req("t-1"), _settings())
    container = manifest["spec"]["containers"][0]
    home = next(e["value"] for e in container["env"] if e["name"] == "HOME")

    assert home == "/state"
    assert any(m["mountPath"] == home for m in container["volumeMounts"])

    # 挂的是**自己的** system 前缀 —— 两个 agent 的对话历史混在一起的话，
    # resume 出来的上下文就是错的
    state = next(c for c in manifest["spec"]["initContainers"] if c["name"] == "oss-state")
    assert "u-1/t-1/system/cli-home" in state["args"][0]


def test_pod_is_locked_down() -> None:
    """非 root、根只读、cap-drop ALL、不挂 ServiceAccount token。

    ★ 不挂 SA token 尤其重要：Pod 被攻破后，一个可用的 SA token 等于
      拿到了集群 API 的入口。会话 Pod 不需要跟 K8s 说话。
    """
    manifest = pod_manifest(_req(), _settings())
    spec = manifest["spec"]
    sec = spec["containers"][0]["securityContext"]

    assert spec["automountServiceAccountToken"] is False
    assert sec["runAsNonRoot"] is True
    # ★ 必须同时给具体 UID：只写 runAsNonRoot 的话，镜像没在 USER 里写数字
    #   UID 就会被 kubelet 拒绝启动（CreateContainerConfigError）。
    assert isinstance(sec["runAsUser"], int)
    assert sec["readOnlyRootFilesystem"] is True
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"]["drop"] == ["ALL"]


def test_grace_period_leaves_room_for_the_three_step_shutdown() -> None:
    """★ 留不够不是慢，是**静默丢事件** —— Pod 被 SIGKILL 时上报还没发出去。"""
    manifest = pod_manifest(_req(), _settings())
    assert manifest["spec"]["terminationGracePeriodSeconds"] >= 30


def test_token_is_injected_from_a_secret_not_the_manifest() -> None:
    """凭证不进 manifest 明文 —— manifest 会被 kubectl get 出来、被日志带走。"""
    manifest = pod_manifest(_req(), _settings())
    env = {e["name"]: e for e in manifest["spec"]["containers"][0]["env"]}
    assert "value" not in env["ATLAS_BRIDGE_TOKEN"]
    assert env["ATLAS_BRIDGE_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "token"
    # 会话绑定：bridge 只接受针对本 Pod 所属会话的指令
    assert env["ATLAS_THREAD_ID"]["value"] == "t-1"


def test_labels_are_the_only_bookkeeping() -> None:
    """★ 状态从 label 派生，不建 pod 表 —— 两处真相必然漂移。"""
    labels = pod_manifest(_req(), _settings())["metadata"]["labels"]
    assert labels[LABEL_MANAGED] == "atlas-cluster"
    assert labels[LABEL_THREAD] == "t-1"
    assert labels[LABEL_USER] == "u-1"


def test_secret_carries_the_same_labels_for_reaping() -> None:
    secret = secret_manifest(_req(), "tok", _settings())
    assert secret["metadata"]["labels"][LABEL_THREAD] == "t-1"
    assert secret["stringData"]["token"] == "tok"


# ──────────────────────────────────────────────── 生命周期


async def test_ensure_is_idempotent_and_reuses_the_token() -> None:
    """★ 复用时不重发 token：现有 Pod 里的 Secret 已经注入过了，
    换一个新的只会让它与 Pod 内的那份对不上。"""
    backend = InMemoryBackend()
    manager = PodManager(backend, _settings())

    first = await manager.ensure(_req())
    second = await manager.ensure(_req())

    assert first.created is True
    assert second.created is False
    assert second.token == first.token
    assert len(backend.pods) == 1


async def test_secret_is_created_before_the_pod() -> None:
    """顺序要紧：反过来的话 Pod 会卡在 ContainerCreating，
    而那个状态从外面看只是「起得慢」。"""
    backend = InMemoryBackend()
    manager = PodManager(backend, _settings())
    await manager.ensure(_req())
    assert backend.secrets  # Secret 在，说明先建的是它（Pod 建失败也不会留下孤儿 Pod）
    assert pod_name_for("t-1") in backend.pods


async def test_release_removes_pod_and_secret_and_is_idempotent() -> None:
    backend = InMemoryBackend()
    manager = PodManager(backend, _settings())
    await manager.ensure(_req())

    await manager.release("t-1")
    assert not backend.pods and not backend.secrets
    await manager.release("t-1")  # 会话删除的级联会重复调，不该炸


async def test_quota_is_per_user_and_refuses_rather_than_queues() -> None:
    """★ 拒绝而不是排队：排队会让「新建会话」的延迟变得不可预测，
    用户看到的是界面转圈。明确报出来，他们可以先关掉几个旧会话。"""
    backend = InMemoryBackend()
    manager = PodManager(backend, _settings(quota_per_user=2))

    await manager.ensure(_req("t-1"))
    await manager.ensure(_req("t-2"))
    with pytest.raises(QuotaExceeded):
        await manager.ensure(_req("t-3"))

    # 配额按**用户**算 —— 另一个租户不受影响
    await manager.ensure(_req("t-4", user_id="u-2"))


async def test_reap_takes_live_threads_not_dead_ones() -> None:
    """★ 输入是「还活着的会话」：cluster 不认识 thread 表，让它自己判断
    哪个该死会要求它反向查 server —— 那条依赖不该有。"""
    backend = InMemoryBackend()
    manager = PodManager(backend, _settings())
    for tid in ("t-1", "t-2", "t-3"):
        await manager.ensure(_req(tid))

    removed = await manager.reap(["t-2"])

    assert sorted(removed) == ["t-1", "t-3"]
    assert set(backend.pods) == {pod_name_for("t-2")}


async def test_reap_ignores_pods_it_does_not_manage() -> None:
    """集群里别人的 Pod 不归它清 —— label 选择器是唯一的作用域。"""
    backend = InMemoryBackend()
    from atlas_cluster.backend import PodRef

    backend.pods["someone-elses"] = PodRef("someone-elses", {"app": "other"})
    manager = PodManager(backend, _settings())
    await manager.ensure(_req("t-1"))

    await manager.reap([])
    assert "someone-elses" in backend.pods


# ──────────────────────────────────────────────── HTTP 面


@pytest.fixture
def cluster_client():
    backend = InMemoryBackend()
    app = create_app(backend=backend, settings=_settings(quota_per_user=1))
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://cluster"), backend


async def test_ensure_endpoint_returns_connection_info(cluster_client) -> None:
    client, _backend = cluster_client
    async with client:
        resp = await client.post("/v1/pods/ensure", json=_req().model_dump())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["token"]
        assert body["url"].startswith("ws://")


async def test_quota_is_429_not_500(cluster_client) -> None:
    """★ 429 而不是 500：调用方据此告诉用户「先关掉几个旧会话」，
    而 500 只会让它当作故障去重试，把配额撞得更死。"""
    client, _backend = cluster_client
    async with client:
        await client.post("/v1/pods/ensure", json=_req("t-1").model_dump())
        resp = await client.post("/v1/pods/ensure", json=_req("t-2").model_dump())
        assert resp.status_code == 429
        assert "上限" in resp.json()["detail"]


async def test_reap_endpoint(cluster_client) -> None:
    client, backend = cluster_client
    async with client:
        await client.post("/v1/pods/ensure", json=_req("t-1").model_dump())
        resp = await client.post("/v1/pods/reap", json={"live_thread_ids": []})
        assert resp.json()["removed"] == ["t-1"]
        assert not backend.pods
