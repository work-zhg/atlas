"""cluster 与 server 之间的边界（acp 详设 §03 的同款论证）。

Pod 生命周期归 cluster —— 这条边界破了的表现不是报错，是 server 侧
**也长出一份 K8s 知识**，于是「谁拥有 Pod 生命周期」有了两个答案，
而两个答案必然漂移。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from atlas_cluster.api import create_app
from atlas_cluster.backend import InMemoryBackend
from atlas_cluster.config import ClusterSettings
from atlas_server.acp.pods import PodProvider
from atlas_server.config import Settings
from atlas_server.domain.spec import CliSpec
from atlas_server.providers.cluster import ClusterPods, ClusterUnavailable
from httpx import ASGITransport

_ROOT = Path(__file__).resolve().parents[1]


def test_cluster_schemas_are_importable_without_fastapi() -> None:
    """★ server 要 import 契约做类型安全的调用，但不该因此拖进 K8s 客户端。

    schemas 只依赖 pydantic；fastapi 与 kubernetes 都在 api / k8s 里。
    """
    code = (
        "import sys, importlib;"
        "importlib.import_module('atlas_cluster.schemas');"
        "bad=[m for m in ('fastapi','kubernetes','kubernetes_asyncio','uvicorn') "
        "if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "", f"cluster.schemas 拖进了重依赖：{proc.stdout}"


def test_server_never_imports_cluster_internals() -> None:
    """server 只许认 atlas_cluster.schemas。

    碰 template / manager / k8s 就等于 server 侧也在拼 manifest ——
    「Pod 长什么样」必须只有一个答案。
    """
    pattern = re.compile(r"atlas_cluster\.(?!schemas)(\w+)")
    offenders: list[str] = []
    for path in (_ROOT / "server" / "src").rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name} → atlas_cluster.{match.group(1)}")
    assert offenders == [], f"server 碰了 cluster 内部：{offenders}"


def test_cluster_never_imports_server() -> None:
    """cluster 是叶子服务：建 Pod 与跑一轮不该缠在一起。"""
    code = (
        "import sys, importlib;"
        "[importlib.import_module('atlas_cluster.' + m) "
        "for m in ('schemas','template','manager','backend','api')];"
        "bad=[m for m in ('atlas_server','atlas_engine','atlas_bridge','sqlalchemy','redis') "
        "if m in sys.modules];"
        "print(','.join(bad))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "", f"cluster 反向依赖了：{proc.stdout}"


def test_cluster_pods_satisfies_the_provider_protocol() -> None:
    """生产实现与步骤 4 的 LocalPods 是同一个协议的两个实现 ——
    AcpRuntime 因此一行不改。"""
    assert isinstance(ClusterPods(Settings(litellm_key="x", default_user_id=_UUID)), PodProvider)


_UUID = "00000000-0000-0000-0000-000000000001"


# ──────────────────────────────────────────────── server → cluster 集成


@pytest.fixture
def stack():
    """真 cluster 服务（内存后端）+ 指向它的 server 侧客户端。"""
    backend = InMemoryBackend()
    app = create_app(backend=backend, settings=ClusterSettings(oss_bucket="b", quota_per_user=1))
    transport = ASGITransport(app=app)

    class InProcessPods(ClusterPods):
        """把 httpx 换成 ASGI 直连 —— 测的仍是真实的请求/响应与状态码映射，
        只是省掉一个真实端口。"""

        def _client(self) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, base_url="http://cluster")

    return InProcessPods(Settings(litellm_key="x", default_user_id=_UUID)), backend


class _Thread:
    def __init__(self, thread_id: str, user_id: str, workspace_thread_id: str) -> None:
        self.id = thread_id
        self.created_by = user_id
        self.workspace_thread_id = workspace_thread_id


_CLI = CliSpec(cli_type="claude-code", adapter="node acp", image="atlas-acp:1")


async def test_server_gets_an_endpoint_from_cluster(stack) -> None:
    pods, backend = stack
    endpoint = await pods.ensure(_Thread("t-1", "u-1", "t-1"), _CLI)
    assert endpoint.url.startswith("ws://")
    assert endpoint.token
    assert len(backend.pods) == 1


async def test_subagent_thread_gets_the_parents_workspace(stack) -> None:
    """★ 端到端地确认两个 thread id 都传到了 cluster。

    少传一个就只能由 cluster 猜，而猜错的表现是子智能体读不到父的产物、
    或者读到了父的技能 —— 两个方向都是回归，且都不报错。
    """
    pods, backend = stack
    await pods.ensure(_Thread("t-child", "u-1", "t-parent"), _CLI)

    pod = next(iter(backend.pods.values()))
    assert pod.labels["atlas/thread-id"] == "t-child"


async def test_quota_message_reaches_the_user_verbatim(stack) -> None:
    """429 原样把 cluster 的话带上来 —— 它已经写明了怎么办。"""
    pods, _backend = stack
    await pods.ensure(_Thread("t-1", "u-1", "t-1"), _CLI)
    with pytest.raises(ClusterUnavailable, match="上限"):
        await pods.ensure(_Thread("t-2", "u-1", "t-2"), _CLI)


async def test_missing_image_fails_before_any_http_call(stack) -> None:
    """配置不全就别去打扰 cluster —— 错误应该指向 agent 配置。"""
    pods, backend = stack
    with pytest.raises(ClusterUnavailable, match="image"):
        await pods.ensure(_Thread("t-1", "u-1", "t-1"), CliSpec(cli_type="x"))
    assert not backend.pods


async def test_unreachable_cluster_is_a_clear_error() -> None:
    """cluster 挂了要说「cluster 不可达」，不是一个 httpx 的栈。"""
    pods = ClusterPods(
        Settings(
            litellm_key="x",
            default_user_id=_UUID,
            cluster_base_url="http://127.0.0.1:1",
            cluster_timeout_s=0.5,
        )
    )
    with pytest.raises(ClusterUnavailable, match="不可达"):
        await pods.ensure(_Thread("t-1", "u-1", "t-1"), _CLI)
