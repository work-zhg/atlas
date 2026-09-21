"""ACP 的 Pod 链路 —— 真集群 + 真对象存储。

    cluster 建 Pod → 挂载器把 GCS 挂进来 → bridge 起 WS → server 连上去
      → initialize / session/new / session/prompt → adapter 往 /workspace 写
      → 文件出现在桶里

没有 k3s 或没配对象存储就整体跳过。

## 这组测什么，不测什么

进程内的那层（会话语义、权限转交、崩溃归类、事件形状）已经由
tests/test_acp_end_to_end.py 覆盖，它用的是同一个 bridge、同一个假 adapter，
只是跑在本进程里。**本组只测跨出进程之后才出现的东西**：

  · Pod 真的能起来（镜像、securityContext、Secret 引用都被 kubelet 接受）
  · 挂载器真的把桶挂进了容器，且 adapter 的 cwd 就是那个挂载点
  · server 拿到的地址真的连得上
  · 两个前缀在真 Pod 里确实来自不同 thread

## 一处测试替身，说清楚

bridge 的生产镜像本机构建不了（没有任何镜像构建工具），所以这里用
python:3.14-slim 加 hostPath 把仓库源码与 venv 挂进去，再拿 tests/fake_adapter.py
当 adapter。被替换掉的只有"镜像怎么来"；Pod、网络、挂载、WS、JSON-RPC
全是真的。**镜像构建本身没有被这组覆盖**。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from . import podkit
from .conftest import REPO_ROOT

_KUBECONFIG = podkit.KUBECONFIG
_NAMESPACE = "atlas-acp-chain"


def _object_storage():
    """真对象存储配置 —— 与 test_oss_gcs.py 同款 opt-in。"""
    from atlas_server.config import Settings

    env = REPO_ROOT / ".env"
    if not env.exists():
        return None
    try:
        settings = Settings(_env_file=env)
    except Exception:  # noqa: BLE001
        return None
    if not (settings.workspace_configured and settings.oss_endpoint):
        return None
    return settings


def _available() -> tuple[bool, str]:
    ok, why = podkit.cluster_available()
    if not ok:
        return False, why
    if _object_storage() is None:
        return False, "未配置真对象存储（.env 缺 OSS_* ）"
    return True, ""


_OK, _WHY = _available()
pytestmark = pytest.mark.skipif(not _OK, reason=_WHY)


# ──────────────────────────────────────────────── 装配


def _settings(**over):
    from atlas_cluster.config import ClusterSettings

    oss = _object_storage()
    return ClusterSettings(
        namespace=_NAMESPACE,
        workspace_mount="fuse",
        oss_bucket=oss.oss_bucket,
        oss_endpoint=oss.oss_endpoint,
        oss_region=oss.oss_region,
        oss_provider="GCS",
        oss_access_key_id=oss.oss_access_key_id.get_secret_value(),
        oss_secret_access_key=oss.oss_access_key_secret.get_secret_value(),
        termination_grace_s=1,
        terminating_wait_s=30.0,
        pod_ready_timeout_s=150.0,
        **over,
    )


def _req(thread_id: str, *, user_id: str, workspace: str | None = None):
    from atlas_cluster.schemas import EnsurePodRequest

    return EnsurePodRequest(
        thread_id=thread_id,
        user_id=user_id,
        # 子会话传父的 thread —— 工作区共享、技能各带各的
        workspace_thread_id=workspace or thread_id,
        image=podkit.BRIDGE_IMAGE,
        adapter=podkit.FAKE_ADAPTER_CMD,
        cli_type="fake",
    )


@pytest.fixture
async def chain():
    """真 backend + manager，用完把本测试建的 Pod 与桶前缀都清干净。"""
    os.environ["KUBECONFIG"] = str(_KUBECONFIG)
    from atlas_cluster.k8s import KubernetesBackend
    from atlas_cluster.manager import PodManager
    from atlas_cluster.template import pod_manifest, secret_manifest
    from atlas_server.providers.filesystem import make_workspace

    await _ensure_namespace()
    backend = KubernetesBackend(_NAMESPACE, kubeconfig=str(_KUBECONFIG))
    settings = _settings()
    manager = PodManager(backend, settings)
    oss = _object_storage()
    started: list[str] = []
    touched: set[tuple[str, str]] = set()

    async def _start(thread_id: str, *, user_id: str, workspace: str | None = None,
                     write_file: str = ""):
        """走真 manager 的 Secret → Pod → 等就绪，只在 manifest 上补测试用的挂载。"""
        from atlas_cluster.pairing import issue_token

        req = _req(thread_id, user_id=user_id, workspace=workspace)
        token = issue_token()
        await backend.apply_secret(secret_manifest(req, token, settings))
        manifest = podkit.inject_test_bridge(pod_manifest(req, settings), write_file=write_file)
        await backend.create_pod(manifest)
        pod = await backend.wait_ready(
            manifest["metadata"]["name"], timeout_s=settings.pod_ready_timeout_s
        )
        started.append(thread_id)
        # ★ 前缀要单独记：子会话的产物落在**父**的前缀下，而父从来没有
        #   自己的 Pod —— 只清 started 的话，父前缀会一直留在桶里累积。
        touched.add((user_id, thread_id))
        touched.add((user_id, workspace or thread_id))
        return f"ws://{pod.ip}:{settings.bridge_port}", token

    try:
        yield _start, manager, backend
    finally:
        for thread_id in started:
            await manager.release(thread_id)
        for user_id, prefix_thread in touched:
            workspace = make_workspace(oss, user_id, prefix_thread)
            if workspace is not None:
                workspace.delete("/skills/", recursive=True)
                workspace.delete("", recursive=True)
        await backend.aclose()


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


async def _one_turn(url: str, token: str, thread_id: str, prompt: str) -> list[dict]:
    """连上 Pod 里的 bridge，跑完整的一轮，返回收到的 update。

    ★ 直接用 AcpChannel 而不是 AcpRuntime：runtime 要 DB 与 Redis，
      而它的语义已经被 test_acp_end_to_end.py 覆盖过了。本组关心的是
      这条链路在**跨进程、跨网络**之后还成不成立。
    """
    from atlas_server.acp.channel import AcpChannel

    updates: list[dict] = []

    async def on_notification(frame: dict) -> None:
        if frame.get("method") == "session/update":
            updates.append(frame["params"]["update"])

    async def on_request(frame: dict):  # noqa: ARG001 - 本组不测权限路径
        return {}

    async with AcpChannel(
        url,
        token=token,
        thread_id=thread_id,
        on_notification=on_notification,
        on_request=on_request,
    ) as channel:
        await channel.request(
            "initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=30
        )
        created = await channel.request(
            "session/new", {"cwd": "/workspace", "mcpServers": []}, timeout=30
        )
        await channel.request(
            "session/prompt",
            {
                "sessionId": created["sessionId"],
                "prompt": [{"type": "text", "text": prompt}],
            },
            timeout=60,
        )
    return updates


# ──────────────────────────────────────────────── 链路


async def test_a_prompt_round_trips_through_a_real_pod(chain) -> None:
    """★ 整条链路最基本的一条：server 连得上 Pod 里的 bridge，一轮跑得完。

    进程内版本一直是绿的，但那里没有 Pod、没有网络、没有 kubelet ——
    第一次真跑就撞出四个被掩盖的问题（地址、就绪、凭据、runAsUser）。
    """
    start, _manager, _backend = chain
    thread_id = f"chain-{uuid.uuid4().hex[:8]}"
    url, token = await start(thread_id, user_id="u-chain")

    updates = await _one_turn(url, token, thread_id, "你好")

    kinds = [u["sessionUpdate"] for u in updates]
    assert "agent_message_chunk" in kinds
    answer = "".join(
        u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"
    )
    assert "做完了" in answer
    # thought 与 answer 分流 —— 跨了网络也不能混
    assert "先看代码" not in answer


async def test_the_adapter_writes_into_the_mounted_bucket(chain) -> None:
    """★ adapter 的 cwd 就是挂进来的 GCS 前缀。

    挂错了的表现不是报错：文件会写进容器本地，Pod 一没就不见了 ——
    用户看到的是"上一轮生成的文件这轮找不到了"，而日志里什么都没有。
    """
    from atlas_server.providers.filesystem import make_workspace

    start, _manager, _backend = chain
    thread_id = f"chain-{uuid.uuid4().hex[:8]}"
    url, token = await start(thread_id, user_id="u-chain", write_file="from-adapter.json")

    await _one_turn(url, token, thread_id, "把这句写进文件")

    workspace = make_workspace(_object_storage(), "u-chain", thread_id)
    # rclone 的写是经 VFS 缓存落盘的，给它一点时间刷上去
    content = await _eventually(lambda: workspace.read("from-adapter.json"))
    assert "把这句写进文件" in json.dumps(json.loads(content), ensure_ascii=False)


async def test_a_subagent_pod_shares_the_parents_workspace(chain) -> None:
    """★ 双前缀在真 Pod 里成立：工作区共享、技能隔离。

    模板测试只能断言 manifest 长什么样；这条断言的是**挂载真的按那个意思
    生效了** —— 子 Pod 写的文件出现在父会话的前缀下。
    """
    from atlas_server.providers.filesystem import make_workspace

    start, _manager, _backend = chain
    parent = f"chain-p-{uuid.uuid4().hex[:8]}"
    child = f"chain-c-{uuid.uuid4().hex[:8]}"

    url, token = await start(
        child, user_id="u-chain", workspace=parent, write_file="child-output.json"
    )
    await _one_turn(url, token, child, "子智能体的产物")

    # 落在**父**的工作区前缀下 —— 这正是委派要的共享
    parent_ws = make_workspace(_object_storage(), "u-chain", parent)
    await _eventually(lambda: parent_ws.read("child-output.json"))

    # 而子会话自己的工作区前缀是空的
    child_ws = make_workspace(_object_storage(), "u-chain", child)
    assert child_ws.search("").keys == []


async def test_the_bridge_rejects_a_wrong_token_over_the_real_network(chain) -> None:
    """★ 入口的唯一闸门是 token —— 集群里没有 NetworkPolicy，网络是平的。

    "在集群里"从来不是安全边界：任何 Pod 都能拨到这个地址。
    """
    from atlas_server.acp.channel import AcpChannel, ChannelClosed

    start, _manager, _backend = chain
    thread_id = f"chain-{uuid.uuid4().hex[:8]}"
    url, _token = await start(thread_id, user_id="u-chain")

    async def _noop(frame: dict):  # noqa: ARG001
        return {}

    with pytest.raises(ChannelClosed):
        async with AcpChannel(
            url,
            token="不是这一个",
            thread_id=thread_id,
            on_notification=_noop,
            on_request=_noop,
        ):
            pass


async def _eventually(read, *, timeout_s: float = 30.0) -> str:
    """等对象在桶里出现。

    ★ rclone 的写走 VFS 缓存，关闭文件之后才异步刷到对象存储 —— 直接断言
      会偶发地失败，而那种失败长得像"功能坏了"。
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = "（从未读到）"
    while asyncio.get_running_loop().time() < deadline:
        result = read()
        if result.error is None:
            return result.file_data["content"]
        last = result.error
        await asyncio.sleep(1.0)
    pytest.fail(f"对象始终没出现在桶里：{last}")
