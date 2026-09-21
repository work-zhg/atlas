"""KubernetesBackend 对着**真集群**（执行环境 §12 的待验证清单）。

没有集群就整体跳过 —— 本地跑 k3s 即可：

    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \\
        --disable=traefik --disable=servicelb --disable=metrics-server \\
        --disable=local-storage --write-kubeconfig-mode=644" sh -
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    pip install kubernetes-asyncio

## 为什么这组测试不可省

内存实现（InMemoryBackend）覆盖的是**主体逻辑**（模板、配额、幂等、孤儿
判定），但它有一处刻意不对齐：delete 立即生效，而 K8s 是异步的优雅终止。
第一次对着真集群冒烟就抓到三个被它掩盖的 bug：

  1. `_token_of` 读的是 `backend.secrets`（内存实现独有的属性）——
     真集群上恒读不到，于是每次复用都静默重签 token，而 Pod 里注入的
     还是旧的：**bridge 握手必然被拒**。
  2. delete 之后 Pod 仍在 list 里（Terminating），`_find` 会把一个正在死的
     Pod 当成可复用；紧接着 `ensure` 还会撞 409。
  3. aiohttp 连接池从不关闭，长跑进程泄漏连接。

下面每一条都钉住其中一个。
"""

from __future__ import annotations

import os

import pytest

from . import podkit

_KUBECONFIG = podkit.KUBECONFIG
_NAMESPACE = "atlas-sessions-test"


_OK, _WHY = podkit.cluster_available()
pytestmark = pytest.mark.skipif(not _OK, reason=_WHY)


@pytest.fixture
async def backend(monkeypatch):
    """真 backend，测完把命名空间里本测试建的东西清干净。"""
    podkit.patch_manager_to_use_the_test_bridge(monkeypatch)
    os.environ["KUBECONFIG"] = str(_KUBECONFIG)
    from atlas_cluster.k8s import KubernetesBackend

    await _ensure_namespace()
    impl = KubernetesBackend(_NAMESPACE, kubeconfig=str(_KUBECONFIG))
    await _wipe(impl)  # 上一条用例的残留不该影响本条
    try:
        yield impl
    finally:
        await _wipe(impl)
        await impl.aclose()


async def _wipe(impl) -> None:
    """删干净并**等它真的消失**。

    ★ K8s 的 delete 是异步的 —— 不等的话上一条用例的 Pod 会漏进下一条，
      表现是「reap 多回收了一个」。踩过一次：那不是产品 bug，是测试隔离。
    """
    import asyncio

    for pod in await impl.list_pods(label_selector="atlas/managed-by=atlas-cluster"):
        await impl.delete_pod(pod.name)
        await impl.delete_secret(f"{pod.name}-auth")

    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if not await impl.list_pods(label_selector="atlas/managed-by=atlas-cluster"):
            return
        await asyncio.sleep(0.3)


async def _ensure_namespace() -> None:
    from kubernetes_asyncio import client, config

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001
        await config.load_kube_config(str(_KUBECONFIG))
    api = client.CoreV1Api()
    try:
        await api.create_namespace({"metadata": {"name": _NAMESPACE}})
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) != 409:
            raise
    finally:
        await api.api_client.close()


def _settings(**over):
    from atlas_cluster.config import ClusterSettings

    return ClusterSettings(
        namespace=_NAMESPACE,
        oss_bucket="atlas-oss",
        # 测试里把优雅终止压短 —— 否则每条用例都要等 30s
        termination_grace_s=1,
        terminating_wait_s=30.0,
        # ★ 生命周期用例挂 none：它们测的是调度、幂等、回收，不该因为缺一套
        #   桶凭据就跑不起来。真正的挂载由 test_acp_pod_chain.py 对着真 GCS 验。
        **{"workspace_mount": "none", **over},
    )


def _req(thread_id: str, *, user_id: str = "u-1", workspace: str | None = None):
    from atlas_cluster.schemas import EnsurePodRequest

    return EnsurePodRequest(
        thread_id=thread_id,
        user_id=user_id,
        workspace_thread_id=workspace or thread_id,
        # ★ 必须是真能起来的 bridge：Ready 现在意味着"bridge 在监听"
        #   （readinessProbe），而 ensure 会等到 Ready。占位镜像过不了。
        image=podkit.BRIDGE_IMAGE,
        adapter=podkit.FAKE_ADAPTER_CMD,
        cli_type="fake",
    )


def _manager(backend):
    from atlas_cluster.manager import PodManager

    return PodManager(backend, _settings())


# ──────────────────────────────────────────────── 契约


async def test_the_api_server_accepts_our_manifest(backend) -> None:
    """★ 最基本的一条：真 API server 认我们渲染的 manifest。

    内存实现只是把字典存起来，不校验任何字段 —— 拼错一个键名、用错一个
    枚举值，它都照收不误。这条是唯一能发现那类错误的测试。
    """
    manager = _manager(backend)
    info = await manager.ensure(_req("t-accept"))
    assert info.created is True

    pods = await backend.list_pods(label_selector="atlas/thread-id=t-accept")
    assert len(pods) == 1
    assert pods[0].terminating is False


async def test_mounts_and_security_survive_the_round_trip(backend) -> None:
    """fuse 模板存进去再读出来，挂载语义与安全上下文都还在。

    ★ API server 会规范化、补默认值、拒绝它不认的字段 —— 读回来确认我们
      写的东西**确实是它接受的那个意思**。内存后端只是把字典存起来，
      拼错一个键名它也照收。

    ★ 这里直接 create_pod 而不走 manager.ensure：ensure 现在会等 Pod 就绪，
      而 fuse 模板要真桶凭据才挂得上。本条测的是 manifest 的契约，不是启动。
    """
    from atlas_cluster.template import pod_manifest
    from kubernetes_asyncio import client

    settings = _settings(workspace_mount="fuse")
    await backend.create_pod(pod_manifest(_req("t-mounts", workspace="t-parent"), settings))

    api = client.CoreV1Api()
    try:
        pod = await api.read_namespaced_pod("atlas-sess-t-mounts", _NAMESPACE)
    finally:
        await api.api_client.close()

    # 两个前缀来自不同 thread —— 「读共享、技能隔离」的结构性来源
    mounters = {c.name: c for c in pod.spec.init_containers}
    assert "u-1/t-parent/workspace" in mounters["oss-workspace"].args[0]
    assert "u-1/t-mounts/skills" in mounters["oss-skills"].args[0]
    # ★ 原生 sidecar：K8s 认这个字段才会先起它、并且不等它退出
    assert mounters["oss-workspace"].restart_policy == "Always"
    assert mounters["oss-workspace"].volume_mounts[0].mount_propagation == "Bidirectional"

    mounts = {m.mount_path: m for m in pod.spec.containers[0].volume_mounts}
    assert mounts["/workspace"].mount_propagation == "HostToContainer"
    assert not mounts["/workspace"].read_only
    assert mounts["/skills"].read_only is True

    sec = pod.spec.containers[0].security_context
    assert sec.run_as_non_root is True
    assert sec.read_only_root_filesystem is True
    assert sec.capabilities.drop == ["ALL"]
    assert pod.spec.automount_service_account_token is False


async def test_ensure_hands_out_a_reachable_address(backend) -> None:
    """★ ensure 返回时 Pod 必须已经就绪，地址必须是能连的。

    早先它在 Pod 还处于 ContainerCreating 时就返回，而且返回的是
    `ws://<pod>.<ns>:8900` —— 一个需要 Service 才能解析的名字，
    而那个 Service 从来没被创建过。两个问题叠在一起的效果是：
    **每个新会话的第一轮都连不上 bridge**。内存后端里 create 即就绪、
    URL 只被字符串比对，所以两个都测不出来。
    """
    import ipaddress

    manager = _manager(backend)
    info = await manager.ensure(_req("t-addr"))

    host = info.url.removeprefix("ws://").split(":")[0]
    ipaddress.ip_address(host)  # 不是 IP 就抛 ValueError

    pods = await backend.list_pods(label_selector="atlas/thread-id=t-addr")
    assert pods[0].ip == host
    assert pods[0].phase == "Running"


async def test_a_pod_that_cannot_start_fails_with_a_usable_reason(backend) -> None:
    """起不来要说清卡在哪，而不是只说超时。

    ★ 镜像拉不动、Secret 缺键、挂载失败长得一模一样，但处理方式完全不同。
      只报"超时"等于把排查推回给人从头查。
    """
    from atlas_cluster.manager import PodManager

    settings = _settings(pod_ready_timeout_s=20.0)
    manager = PodManager(backend, settings)
    req = _req("t-nopull")
    req.image = "example.invalid/nonexistent:0"

    with pytest.raises(TimeoutError, match="Pull|Image|拉取|容器"):
        await manager.ensure(req)


async def test_token_is_injected_from_the_secret(backend) -> None:
    """env 引用 Secret，而不是把 token 写进 manifest 明文。"""
    from kubernetes_asyncio import client

    manager = _manager(backend)
    info = await manager.ensure(_req("t-secret"))

    api = client.CoreV1Api()
    try:
        pod = await api.read_namespaced_pod("atlas-sess-t-secret", _NAMESPACE)
    finally:
        await api.api_client.close()

    env = {e.name: e for e in pod.spec.containers[0].env}
    assert env["ATLAS_BRIDGE_TOKEN"].value is None
    assert env["ATLAS_BRIDGE_TOKEN"].value_from.secret_key_ref.key == "token"
    # Secret 里存的确实是这次签发的那一份
    stored = await backend.read_secret_value("atlas-sess-t-secret-auth", "token")
    assert stored == info.token


# ──────────────────────────────────────────────── 被内存实现掩盖的三个 bug


async def test_reuse_returns_the_same_token_as_the_pod_has(backend) -> None:
    """★ bug 1 的回归钉。

    复用既有 Pod 时必须读回**Pod 里注入的那一份** token。重签一个新的
    等于 bridge 握手必挂，而且错误发生在连接时，离根因很远。
    """
    manager = _manager(backend)
    first = await manager.ensure(_req("t-token"))
    second = await manager.ensure(_req("t-token"))

    assert second.created is False
    assert second.token == first.token
    assert await backend.read_secret_value("atlas-sess-t-token-auth", "token") == first.token


async def test_a_terminating_pod_is_not_reused(backend) -> None:
    """★ bug 2 的回归钉。

    K8s 的 delete 是异步的：返回之后 Pod 还在 list 里、phase 仍是 Running，
    只是多了 deletionTimestamp。把它当成可复用的话，连上去才发现它在关闭。
    """
    manager = _manager(backend)
    await manager.ensure(_req("t-term"))
    await backend.delete_pod("atlas-sess-t-term")

    pods = await backend.list_pods(label_selector="atlas/thread-id=t-term")
    if pods:  # 还没被完全收割 —— 正是要测的那一刻
        assert pods[0].terminating is True
        assert await manager._find("t-term") is None, "正在终止的 Pod 被当成了可复用"


async def test_recreating_right_after_release_does_not_conflict(backend) -> None:
    """★ bug 2 的后半：同名 Pod 在旧的消失前建不出来（409）。

    「删了会话立刻又开一个」是很常见的动作 —— ensure 必须等旧的走干净，
    否则这条路径会随机失败。
    """
    manager = _manager(backend)
    await manager.ensure(_req("t-recreate"))
    await manager.release("t-recreate")

    info = await manager.ensure(_req("t-recreate"))
    assert info.created is True


async def test_backend_closes_its_connection_pool(backend) -> None:
    """★ bug 3 的回归钉：长跑进程不关会泄漏连接。"""
    await backend.list_pods(label_selector="atlas/managed-by=atlas-cluster")
    assert backend._api is not None
    await backend.aclose()
    assert backend._api is None


# ──────────────────────────────────────────────── 生命周期


async def test_reap_removes_orphans_on_a_real_cluster(backend) -> None:
    manager = _manager(backend)
    await manager.ensure(_req("t-live"))
    await manager.ensure(_req("t-dead"))

    removed = await manager.reap(["t-live"])
    assert removed == ["t-dead"]  # fixture 保证了本用例开始时是干净的

    # 用 _find 而不是 list：Terminating 的还会在 list 里待一会儿
    assert await manager._find("t-dead") is None
    assert await manager._find("t-live") is not None


async def test_quota_counts_real_pods(backend) -> None:
    from atlas_cluster.manager import PodManager, QuotaExceeded

    # ★ 经 _settings 而不是自己拼 ClusterSettings —— 漏掉 workspace_mount
    #   的话会落回默认的 fuse，而这里没有桶凭据，挂载器起不来：
    #   配额还没判到就先 Init:CrashLoopBackOff 了。
    manager = PodManager(backend, _settings(quota_per_user=1))

    await manager.ensure(_req("t-q1", user_id="u-quota"))
    with pytest.raises(QuotaExceeded):
        await manager.ensure(_req("t-q2", user_id="u-quota"))
